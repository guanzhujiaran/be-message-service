"""事件提醒对象模型（点赞 / 回复 / @提及 / 审核驳回 / 举报下架 / 举报结果）。

2.50.0 起从「静态方法工具类」重构为**面向对象**的事件处理器：

- 每种 ``EventTypeEnum`` 对应一个处理器类（``LikeEvent`` / ``ReplyEvent`` / ...），
  统一继承自虚基类 ``BaseEvent``；
- 写路径（上报）与跨类型的读路径（聚合 / 明细 / msgfeed / 已读 / 计数）的公共逻辑下沉到
  ``BaseEvent``，**只有「按类型差异化」的逻辑（主要是 msgfeed 内容体构建）被抽成抽象方法
  ``build_msgfeed_content``**，由子类实现；
- 业务方不再直接调 ``EventService``，而是 ``BaseEvent.from_req(req).report(session)``，
  或 ``ReplyEvent(mid=..., ...).report(session)`` 这样按对象操作，类型含义一目了然。

设计要点（沿用旧 ``EventService`` 的契约）：

- **上报（write）**：先过消息设置闸门 → 自赞过滤 → 计算幂等键 → 落库；
- **聚合读（read）**：按 ``source_type + source_id`` 分组，把「12 人赞了同一条动态」
  收敛成一张卡片；
- **已读管理**：支持按 id / 类型 / 聚合分组三种粒度。

幂等：``dedup_key``（唯一索引）由 ``mid:event_type:actor_mid:source_type:source_id:biz_id``
摘要而来。MQ 重投、前端重试、爬虫重复扫描都会被数据库直接拦掉。
"""

from __future__ import annotations

import contextlib
import hashlib
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import ClassVar

from loguru import logger
from sqlalchemy import case, func, tuple_
from sqlalchemy.exc import IntegrityError
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.db import (
    CommentContent,
    CommentIndex,
    EventMessage,
    EventReadCursor,
    UserFollow,
)
from app.models.db.moment_tbl import TMoment
from app.models.enums import (
    CommentStateEnum,
    EventTypeEnum,
    FollowStatusEnum,
    SourceTypeEnum,
)
from bili_common.models.interaction import InteractionBizTypeEnum
from app.models.schemas import (
    EventAggregateItem,
    EventItem,
    EventListResp,
    EventMsgfeedContent,
    EventMsgfeedCursor,
    EventMsgfeedItem,
    EventMsgfeedSection,
    EventReadReq,
    EventReadResp,
    EventReportReq,
    EventReportResp,
    EventUserBrief,
)
from app.services.user.pptr_user import PptrUserService
from app.services.message.setting import SettingService

# 每张聚合卡片（aggregate 接口）最多展示的触发者头像数
_MAX_ACTORS_PER_GROUP = 3
# msgfeed 单条 users[] 后端返回上限（前端 B 站样式最多展示 2 个，后端多给便于扩展）
_MAX_USERS_PER_ITEM = 4
# 为了在内存里凑齐每组的头像，单次最多回捞的明细条数（防止大分组撑爆内存）
_ACTOR_SCAN_LIMIT = 500

# source_type（事件来源实体类型）→ resource_type（互动资源类型，InteractionBizTypeEnum 1~6）
# 用于 msgfeed item 统一承载动态 / 抽奖卡片 / RPA 操作等资源的互动通知。
_SOURCE_TYPE_TO_BIZ_TYPE: dict[SourceTypeEnum, InteractionBizTypeEnum] = {
    SourceTypeEnum.DYNAMIC: InteractionBizTypeEnum.DYNAMIC,
    SourceTypeEnum.LOTTERY: InteractionBizTypeEnum.LOTTERY,
}


# ==================== msgfeed 构建上下文 ====================


@dataclass
class MsgfeedBuildContext:
    """一次 msgfeed 聚合查询中，预回捞好的共享数据。

    由 ``BaseEvent.list_msgfeed`` 在拿到本页所有分组后**统一回查一次**
    （用户 / 关注态 / 评论索引 / 评论正文 / 动态缓存），避免在循环里发查询；
    每个事件处理器在 ``build_msgfeed_content`` 里直接读这些内存映射即可。
    """

    session: AsyncSession
    user_map: dict[int, object] = field(default_factory=dict)
    follow_targets: set[int] = field(default_factory=set)
    comment_index: dict[int, CommentIndex] = field(default_factory=dict)
    comment_content: dict[int, str] = field(default_factory=dict)
    dyn_cache: dict[int, object] = field(default_factory=dict)

    def user_brief(self, actor_mid: int) -> EventUserBrief:
        """按 mid 回查得到的触发者简况（昵称 / 头像 / 粉丝数 / 是否关注）。"""
        info = self.user_map.get(int(actor_mid))
        return EventUserBrief(
            mid=actor_mid,
            nickname=(info.uname if info else None),
            avatar=(info.avatar if info else None),
            fans=int(getattr(info, "follower_count", 0) or 0) if info else 0,
            follow=actor_mid in self.follow_targets,
        )


# ==================== 原资源实时回捞（读取时补全标题 / 封面）====================


async def _resolve_source_meta(
    session: AsyncSession,
    source_type: SourceTypeEnum | None,
    source_id: str | None,
    biz_id: str | None = None,
    dyn_cache: dict[int, object] | None = None,
) -> tuple[str, str]:
    """按 source_type + source_id 实时回捞原资源的标题 / 封面。

    - DYNAMIC：直接读 TMoment（与事件表同库）；
    - COMMENT：先按 biz_id 取评论索引拿到 oid，再读 TMoment；
    - LOTTERY：走 RPA RPC 取详情（弱依赖，失败返回空）；
    - 其余类型（VIDEO / ARTICLE / OTHER）本地无原资源，返回空串。

    仅依赖 id，不在事件表冗余存储快照，省空间也更不易过期。

    注意：**不拼接跳转 uri**。业务由 ``business`` + ``type`` 表达，
    uri 由前端按这两个字段自行决定，后端不再产出链接。
    """
    st = source_type
    if isinstance(st, int):
        with contextlib.suppress(Exception):
            st = SourceTypeEnum(st)

    if st is SourceTypeEnum.DYNAMIC and source_id:
        dyn = await _load_dynamic(session, source_id, dyn_cache)
        if dyn is not None:
            return (dyn.contentText or "", _first_pic(dyn.contentJson) or "")

    if st is SourceTypeEnum.COMMENT and biz_id:
        if biz_id.isdigit():
            idx = await session.get(CommentIndex, int(biz_id))
            if idx is not None and idx.oid:
                dyn = await _load_dynamic(session, str(idx.oid), dyn_cache)
                if dyn is not None:
                    return (dyn.contentText or "", _first_pic(dyn.contentJson) or "")
        return ("", "")

    if st is SourceTypeEnum.LOTTERY and source_id:
        try:
            from app.services.infrastructure.rpa_rpc import rpa_rpc_client

            detail = await rpa_rpc_client.get_resource_detail(
                InteractionBizTypeEnum.LOTTERY.to_text(), source_id
            )
            if detail is not None:
                name = getattr(detail, "name", None) or ""
                cover = getattr(detail, "cover", None) or ""
                return (str(name), str(cover))
        except Exception:  # noqa: BLE001
            logger.debug("回捞 lottery 资源详情失败（弱依赖，忽略）", exc_info=True)
        return ("", "")

    return ("", "")


async def _load_dynamic(
    session: AsyncSession,
    dyn_id: str | None,
    dyn_cache: dict[int, object] | None,
) -> "TMoment | None":
    """按 dynId 读动态；软删动态视为不存在；带内存缓存避免一页内重复查。"""
    if not dyn_id or not str(dyn_id).isdigit():
        return None
    did = int(dyn_id)
    if dyn_cache is not None and did in dyn_cache:
        cached = dyn_cache[did]
        return cached if isinstance(cached, TMoment) else None
    dyn = await session.get(TMoment, did)
    if dyn_cache is not None:
        dyn_cache[did] = dyn
    if dyn is not None and getattr(dyn, "deletedAt", None) is not None:
        return None
    return dyn


def _first_pic(content_json: object) -> str:
    """从动态富文本正文里取第一张图片（外链图 / 资源卡封面）。"""
    if not content_json:
        return ""
    nodes: list = []
    if isinstance(content_json, dict):
        nodes = content_json.get("paragraphs", []) or content_json.get("nodes", [])
    elif isinstance(content_json, list):
        nodes = content_json
    for node in nodes:
        if not isinstance(node, dict):
            continue
        pic = node.get("picMeta")
        if isinstance(pic, dict) and pic.get("imgUrl"):
            return str(pic["imgUrl"])
        if node.get("cover"):
            return str(node["cover"])
    return ""


def build_dedup_key(
    mid: int,
    event_type: EventTypeEnum,
    actor_mid: int,
    source_type: SourceTypeEnum,
    source_id: str,
    biz_id: str | None = None,
) -> str:
    """计算事件幂等键（兼容旧调用）。

    未传 ``biz_id`` 时，同一个人对同一实体的同类行为只会记一条
    （反复点赞取消点赞不会刷屏）；传了 ``biz_id``（如评论 id）则按业务实体区分，
    同一个人的多条回复各记一条。
    """
    raw = f"{mid}:{event_type}:{actor_mid}:{source_type}:{source_id}:{biz_id or ''}"
    return hashlib.md5(raw.encode("utf-8")).hexdigest()


# ==================== 虚基类 ====================


class BaseEvent(ABC):
    """事件处理器的虚基类。

    子类**必须**声明类属性 ``event_type``（对应 ``EventTypeEnum`` 的一个成员），
    并（直接或通过 ``GenericEvent``）实现抽象方法 ``build_msgfeed_content``。

    设计分工：

    - 写路径 ``report``、跨类型读路径（``aggregate`` / ``list_detail`` /
      ``list_msgfeed`` / ``mark_read`` / ``delete`` / ``count_unread`` /
      ``count_unread_by_type``）是**与类型无关**的公共逻辑，由本基类直接提供；
    - 只有「该类型在 msgfeed 聚合条目里长什么样」因类型而异，抽成抽象方法
      ``build_msgfeed_content``，由子类实现（如 ``ReplyEvent`` 需要实时回捞
      评论层级关系 / 正文，其余类型走通用实现）。
    """

    # 子类必须覆盖：该处理器对应的事件类型
    event_type: ClassVar[EventTypeEnum]

    def __init__(
        self,
        *,
        mid: int,
        actor_mid: int,
        source_type: SourceTypeEnum = SourceTypeEnum.OTHER,
        source_id: str,
        biz_id: str | None = None,
        content: str | None = None,
    ) -> None:
        self.mid = mid
        self.actor_mid = actor_mid
        self.source_type = source_type
        self.source_id = source_id
        self.biz_id = biz_id
        self.content = content

    # ==================== 工厂 ====================

    @classmethod
    def from_req(cls, req: EventReportReq) -> "BaseEvent":
        """从上报请求构造对应类型的事件处理器对象（最常用的入口）。"""
        handler_cls = EVENT_REGISTRY.get(req.event_type, GenericEvent)
        return handler_cls(
            mid=req.mid,
            actor_mid=req.actor_mid,
            source_type=req.source_type,
            source_id=req.source_id,
            biz_id=req.biz_id,
            content=req.content,
        )

    @classmethod
    def _for_type(cls, event_type: EventTypeEnum) -> "BaseEvent":
        """读路径：按分组里的 event_type 取出对应处理器实例（mid 占位，仅用于构建内容）。"""
        handler_cls = EVENT_REGISTRY.get(event_type, GenericEvent)
        return handler_cls(mid=0, actor_mid=0, source_id="")

    # ==================== 写路径 ====================

    def build_dedup_key(self) -> str:
        """计算本事件的幂等键。"""
        raw = (
            f"{self.mid}:{self.event_type}:{self.actor_mid}:"
            f"{self.source_type}:{self.source_id}:{self.biz_id or ''}"
        )
        return hashlib.md5(raw.encode("utf-8")).hexdigest()

    async def report(self, session: AsyncSession) -> EventReportResp:
        """上报一条用户行为事件（落库即送达，由接收方轮询读取）。

        顺序：消息设置闸门 → 自赞过滤 → dedup_key 幂等 → 落库。
        """
        # 闸门一：用户是否愿意接收这类提醒
        accepted = await SettingService.accept_event(session, self.mid, self.event_type)
        if not accepted:
            logger.debug(f"用户 {self.mid} 已关闭 {self.event_type} 提醒，跳过")
            return EventReportResp(accepted=False, duplicated=False)

        # 闸门二：不给自己发提醒
        if self.mid == self.actor_mid:
            return EventReportResp(accepted=False, duplicated=False)

        dedup_key = self.build_dedup_key()
        row = EventMessage(
            mid=self.mid,
            event_type=self.event_type,
            source_type=self.source_type,
            source_id=self.source_id,
            biz_id=self.biz_id,
            actor_mid=self.actor_mid,
            content=self.content,
            dedup_key=dedup_key,
        )
        session.add(row)
        try:
            await session.commit()
            await session.refresh(row)
        except IntegrityError:
            # 命中 dedup_key 唯一索引：重复上报，直接返回既有事件
            await session.rollback()
            existing = (
                await session.exec(
                    select(EventMessage).where(EventMessage.dedup_key == dedup_key)
                )
            ).one_or_none()
            return EventReportResp(
                accepted=True,
                event_id=existing.id if existing else None,
                duplicated=True,
            )

        return EventReportResp(
            accepted=True,
            event_id=row.id,
            duplicated=False,
        )

    # ==================== 抽象方法：子类按类型实现 ====================

    @abstractmethod
    async def build_msgfeed_content(
        self,
        ctx: MsgfeedBuildContext,
        latest: EventMessage,
        rows: list[EventMessage],
    ) -> EventMsgfeedContent:
        """构建该类型在 msgfeed 聚合条目中的内容实体（``item`` 字段）。

        ``latest`` 为组内最新一条事件，``rows`` 为该组去重后的全部触发明细
        （用于需要组内上下文的类型，如回复需要回捞被回复评论正文）。
        """

    # ==================== 公共辅助（供子类 / 读路径复用）====================

    def _build_users(
        self, ctx: MsgfeedBuildContext, rows: list[EventMessage]
    ) -> list[EventUserBrief]:
        """按触发时间倒序去重后的触发者头像列表（所有类型通用）。"""
        users: list[EventUserBrief] = []
        seen: set[int] = set()
        for r in rows:
            if r.actor_mid in seen:
                continue
            seen.add(r.actor_mid)
            users.append(ctx.user_brief(r.actor_mid))
            if len(users) >= _MAX_USERS_PER_ITEM:
                break
        return users

    async def _generic_content(
        self, ctx: MsgfeedBuildContext, latest: EventMessage
    ) -> EventMsgfeedContent:
        """「非评论」类事件的通用 msgfeed 内容体（点赞 / @ / 审核 / 举报等）。"""
        biz_id = latest.biz_id or ""
        resource_type = _SOURCE_TYPE_TO_BIZ_TYPE.get(
            latest.source_type, InteractionBizTypeEnum.DYNAMIC
        ).value
        resource_id = biz_id if biz_id.isdigit() else ""
        title, image = await _resolve_source_meta(
            ctx.session, latest.source_type, latest.source_id, latest.biz_id, ctx.dyn_cache
        )
        return EventMsgfeedContent(
            item_id=latest.id or 0,
            type=int(self.event_type),
            business=latest.source_type.value if latest.source_type else 0,
            resource_type=resource_type,
            resource_id=resource_id,
            root_id="",
            source_id="",
            target_id="",
            title=title,
            desc=latest.content or "",
            image=image,
            source_content="",
            target_content="",
            comment_deleted=False,
            ctime=int(latest.created_at.timestamp()) if latest.created_at else 0,
        )

    # ==================== 跨类型读路径（集合操作，保留为类方法）====================

    @classmethod
    async def aggregate(
        cls,
        session: AsyncSession,
        mid: int,
        event_type: EventTypeEnum | None = None,
        page_num: int = 1,
        page_size: int = 20,
        only_unread: bool = False,
    ) -> tuple[list[EventAggregateItem], int]:
        """按 source_type + source_id 聚合展示。"""
        conditions = [EventMessage.mid == mid, EventMessage.is_deleted == False]
        if event_type is not None:
            conditions.append(EventMessage.event_type == event_type)
        if only_unread:
            conditions.append(EventMessage.is_read == False)

        group_cols = (
            EventMessage.event_type,
            EventMessage.source_type,
            EventMessage.source_id,
        )

        subq = (
            select(*group_cols).where(*conditions).group_by(*group_cols).subquery()
        )
        total = int(
            (await session.exec(select(func.count()).select_from(subq))).one() or 0
        )

        unread_expr = func.sum(case((EventMessage.is_read == False, 1), else_=0))
        stmt = (
            select(
                *group_cols,
                func.count().label("cnt"),
                unread_expr.label("unread"),
                func.max(EventMessage.id).label("latest_id"),
            )
            .where(*conditions)
            .group_by(*group_cols)
            .order_by(func.max(EventMessage.id).desc())
            .offset((page_num - 1) * page_size)
            .limit(page_size)
        )
        groups = (await session.exec(stmt)).all()
        if not groups:
            return [], total

        group_keys = [(g[0], g[1], g[2]) for g in groups]
        detail_stmt = (
            select(EventMessage)
            .where(
                *conditions,
                tuple_(*group_cols).in_(group_keys),  # type: ignore[arg-type]
            )
            .order_by(EventMessage.id.desc())  # type: ignore[union-attr]
            .limit(_ACTOR_SCAN_LIMIT)
        )
        details = list((await session.exec(detail_stmt)).all())

        bucket: dict[tuple, list[EventMessage]] = {}
        for row in details:
            bucket.setdefault(
                (row.event_type, row.source_type, row.source_id), []
            ).append(row)

        actor_mids: set[int] = {r.actor_mid for r in details}
        user_map = await PptrUserService.get_many(actor_mids)

        items: list[EventAggregateItem] = []
        for etype, stype, sid, cnt, unread, latest_id in groups:
            rows = bucket.get((etype, stype, sid), [])
            latest = rows[0] if rows else None
            actors: list[EventUserBrief] = []
            seen: set[int] = set()
            for r in rows:
                if r.actor_mid in seen:
                    continue
                seen.add(r.actor_mid)
                info = user_map.get(r.actor_mid)
                actors.append(
                    EventUserBrief(
                        mid=r.actor_mid,
                        nickname=info.uname if info else None,
                        avatar=info.avatar if info else None,
                        fans=int(getattr(info, "follower_count", 0) or 0) if info else 0,
                        follow=False,
                    )
                )
                if len(actors) >= _MAX_ACTORS_PER_GROUP:
                    break
            title, image = await _resolve_source_meta(
                session, stype, sid, latest.biz_id if latest else None
            )
            items.append(
                EventAggregateItem(
                    event_type=etype,
                    source_type=stype,
                    source_id=sid,
                    biz_id=latest.biz_id if latest else None,
                    title=title,
                    image=image,
                    count=int(cnt or 0),
                    unread_count=int(unread or 0),
                    actors=actors,
                    latest_event_id=int(latest_id or 0),
                    latest_desc=latest.content if latest else None,
                    latest_at=latest.created_at if latest else None,
                )
            )
        return items, total

    @classmethod
    async def list_detail(
        cls,
        session: AsyncSession,
        mid: int,
        event_type: EventTypeEnum | None = None,
        source_type: SourceTypeEnum | None = None,
        source_id: str | None = None,
        page_num: int = 1,
        page_size: int = 20,
        only_unread: bool = False,
    ) -> tuple[list[EventItem], int]:
        """查看某个聚合分组下的事件明细（点开卡片后的列表）。"""
        conditions = [EventMessage.mid == mid, EventMessage.is_deleted == False]
        if event_type is not None:
            conditions.append(EventMessage.event_type == event_type)
        if source_type is not None:
            conditions.append(EventMessage.source_type == source_type)
        if source_id is not None:
            conditions.append(EventMessage.source_id == source_id)
        if only_unread:
            conditions.append(EventMessage.is_read == False)

        total = int(
            (
                await session.exec(
                    select(func.count()).select_from(EventMessage).where(*conditions)
                )
            ).one()
            or 0
        )
        stmt = (
            select(EventMessage)
            .where(*conditions)
            .order_by(EventMessage.id.desc())  # type: ignore[union-attr]
            .offset((page_num - 1) * page_size)
            .limit(page_size)
        )
        rows = (await session.exec(stmt)).all()
        dyn_cache: dict[int, object] = {}
        items: list[EventItem] = []
        for r in rows:
            title, image = await _resolve_source_meta(
                session, r.source_type, r.source_id, r.biz_id, dyn_cache
            )
            items.append(
                EventItem(
                    id=r.id or 0,
                    event_type=r.event_type,
                    source_type=r.source_type,
                    source_id=r.source_id,
                    biz_id=r.biz_id,
                    title=title,
                    image=image,
                    actor_mid=r.actor_mid,
                    desc=r.content,
                    is_read=r.is_read,
                    created_at=r.created_at,
                )
            )
        return items, total

    @classmethod
    async def list_msgfeed(
        cls,
        session: AsyncSession,
        mid: int,
        event_type: EventTypeEnum | None = None,
        cursor_id: int | None = None,
        page_size: int = 20,
        only_unread: bool = False,
    ) -> EventListResp:
        """按 source_type + source_id 聚合的 B 站式互动提醒列表。

        聚合分组键为 ``(event_type, source_type, source_id)``，每组的内容体
        交由 ``_for_type(event_type).build_msgfeed_content(...)`` 按类型构建。
        """
        conditions = [EventMessage.mid == mid, EventMessage.is_deleted == False]
        if event_type is not None:
            conditions.append(EventMessage.event_type == event_type)
        if only_unread:
            conditions.append(EventMessage.is_read == False)
        if cursor_id is not None:
            conditions.append(EventMessage.id < cursor_id)

        group_cols = (
            EventMessage.event_type,
            EventMessage.source_type,
            EventMessage.source_id,
        )

        unread_expr = func.sum(case((EventMessage.is_read == False, 1), else_=0))
        stmt = (
            select(
                *group_cols,
                func.count().label("cnt"),
                unread_expr.label("unread"),
                func.max(EventMessage.id).label("latest_id"),
            )
            .where(*conditions)
            .group_by(*group_cols)
            .order_by(func.max(EventMessage.id).desc())
            .limit(page_size)
        )
        groups = (await session.exec(stmt)).all()
        if not groups:
            return EventListResp(
                latest=EventMsgfeedSection(
                    cursor=EventMsgfeedCursor(is_end=True, id=None, time=None), items=[]
                ),
                total=EventMsgfeedSection(
                    cursor=EventMsgfeedCursor(is_end=True, id=None, time=None), items=[]
                ),
            )

        group_keys = [(g[0], g[1], g[2]) for g in groups]
        detail_stmt = (
            select(EventMessage)
            .where(
                *conditions,
                tuple_(*group_cols).in_(group_keys),  # type: ignore[arg-type]
            )
            .order_by(EventMessage.id.desc())  # type: ignore[union-attr]
            .limit(_ACTOR_SCAN_LIMIT)
        )
        details = list((await session.exec(detail_stmt)).all())

        bucket: dict[tuple, list[EventMessage]] = {}
        for row in details:
            bucket.setdefault(
                (row.event_type, row.source_type, row.source_id), []
            ).append(row)

        actor_mids: set[int] = set()
        for rows in bucket.values():
            for r in rows:
                actor_mids.add(r.actor_mid)
        user_map = await PptrUserService.get_many(actor_mids)
        dyn_cache: dict[int, object] = {}

        # ---- 评论关系 / 正文 / 点赞态 / 关注态，读取时实时回捞 ----
        # 只处理 REPLY 类型（且来源非 DYNAMIC）的 biz_id（触发评论 rpid）。
        reply_biz_ids: set[str] = set()
        for etype, stype, sid, _cnt, _unread, _latest_id in groups:
            if etype != EventTypeEnum.REPLY or stype == SourceTypeEnum.DYNAMIC:
                continue
            for r in bucket.get((etype, stype, sid), []):
                if r.biz_id:
                    reply_biz_ids.add(r.biz_id)

        reply_biz_ints = [int(b) for b in reply_biz_ids if b.isdigit()]
        comment_index: dict[int, CommentIndex] = {}
        comment_content: dict[int, str] = {}
        if reply_biz_ints:
            idx_rows = (
                await session.exec(
                    select(CommentIndex).where(CommentIndex.rpid.in_(reply_biz_ints))
                )
            ).all()
            comment_index = {row.rpid: row for row in idx_rows}
            all_rpids = set(reply_biz_ints)
            for row in idx_rows:
                if row.root:
                    all_rpids.add(row.root)
                if row.parent:
                    all_rpids.add(row.parent)
            content_rows = (
                await session.exec(
                    select(CommentContent).where(CommentContent.rpid.in_(all_rpids))
                )
            ).all()
            comment_content = {row.rpid: row.message for row in content_rows}

        follow_targets: set[int] = set()
        if actor_mids:
            follow_rows = (
                await session.exec(
                    select(UserFollow).where(
                        UserFollow.mid == mid,
                        UserFollow.target_mid.in_(actor_mids),  # type: ignore[arg-type]
                        UserFollow.status == FollowStatusEnum.FOLLOWING,
                    )
                )
            ).all()
            follow_targets = {row.target_mid for row in follow_rows}

        ctx = MsgfeedBuildContext(
            session=session,
            user_map=user_map,
            follow_targets=follow_targets,
            comment_index=comment_index,
            comment_content=comment_content,
            dyn_cache=dyn_cache,
        )

        total_items: list[EventMsgfeedItem] = []
        for etype, stype, sid, cnt, _unread, latest_id in groups:
            handler = cls._for_type(etype)
            rows = bucket.get((etype, stype, sid), [])
            latest = rows[0]
            users = handler._build_users(ctx, rows)
            content = await handler.build_msgfeed_content(ctx, latest, rows)
            total_items.append(
                EventMsgfeedItem(
                    id=latest.id or 0,
                    users=users,
                    item=content,
                    counts=int(cnt or 0),
                    notice_state=0,
                )
            )

        next_conditions = [EventMessage.mid == mid, EventMessage.is_deleted == False]
        if event_type is not None:
            next_conditions.append(EventMessage.event_type == event_type)
        if only_unread:
            next_conditions.append(EventMessage.is_read == False)
        if total_items:
            last_id = total_items[-1].id
            next_conditions.append(EventMessage.id < last_id)
            has_more = bool(
                (
                    await session.exec(
                        select(func.count())
                        .select_from(EventMessage)
                        .where(*next_conditions)
                    )
                ).one()
            )
        else:
            has_more = False

        cursor = EventMsgfeedCursor(
            is_end=not has_more,
            id=total_items[-1].id if total_items else None,
            time=datetime.fromtimestamp(total_items[-1].item.ctime)
            if (total_items and total_items[-1].item.ctime)
            else None,
        )

        latest_cursor = EventMsgfeedCursor(
            is_end=True,
            id=total_items[0].id if total_items else None,
            time=(
                datetime.fromtimestamp(total_items[0].item.ctime)
                if (total_items and total_items[0].item.ctime)
                else None
            ),
        )
        return EventListResp(
            latest=EventMsgfeedSection(cursor=latest_cursor, items=total_items[:1]),
            total=EventMsgfeedSection(cursor=cursor, items=total_items),
        )

    @classmethod
    async def mark_read(
        cls, session: AsyncSession, mid: int, req: EventReadReq
    ) -> EventReadResp:
        """标记已读，支持 id / 类型 / 聚合分组三种粒度。"""
        table = EventMessage.__table__
        conditions = [table.c.mid == mid, table.c.is_read == False]

        if req.event_ids:
            conditions.append(table.c.id.in_(req.event_ids))
        else:
            if req.event_type is not None:
                conditions.append(table.c.event_type == req.event_type)
            if req.source_type is not None:
                conditions.append(table.c.source_type == req.source_type)
            if req.source_id is not None:
                conditions.append(table.c.source_id == req.source_id)

        now = datetime.now()
        result = await session.exec(
            table.update().where(*conditions).values(  # type: ignore[call-overload]
                is_read=True, read_at=now, updated_at=now
            )
        )
        affected = int(getattr(result, "rowcount", 0) or 0)

        if req.event_type is not None and not req.event_ids:
            await cls._advance_cursor(session, mid, req.event_type)

        await session.commit()
        return EventReadResp(
            affected=affected,
            unread_count=await cls.count_unread(session, mid, req.event_type),
        )

    @classmethod
    async def delete(cls, session: AsyncSession, mid: int, event_ids: list[int]) -> int:
        if not event_ids:
            return 0
        table = EventMessage.__table__
        result = await session.exec(
            table.update()  # type: ignore[call-overload]
            .where(table.c.mid == mid, table.c.id.in_(event_ids))
            .values(is_deleted=True, updated_at=datetime.now())
        )
        await session.commit()
        return int(getattr(result, "rowcount", 0) or 0)

    @classmethod
    async def count_unread(
        cls, session: AsyncSession, mid: int, event_type: EventTypeEnum | None = None
    ) -> int:
        conditions = [
            EventMessage.mid == mid,
            EventMessage.is_read == False,
            EventMessage.is_deleted == False,
        ]
        if event_type is not None:
            conditions.append(EventMessage.event_type == event_type)
        stmt = select(func.count()).select_from(EventMessage).where(*conditions)
        return int((await session.exec(stmt)).one() or 0)

    @classmethod
    async def count_unread_by_type(cls, session: AsyncSession, mid: int) -> dict[str, int]:
        """一次查询拿到各类型未读数（前端红点）。"""
        stmt = (
            select(EventMessage.event_type, func.count())
            .where(
                EventMessage.mid == mid,
                EventMessage.is_read == False,
                EventMessage.is_deleted == False,
            )
            .group_by(EventMessage.event_type)
        )
        return {str(etype): int(cnt) for etype, cnt in (await session.exec(stmt)).all()}

    @classmethod
    async def _advance_cursor(
        cls, session: AsyncSession, mid: int, event_type: EventTypeEnum
    ) -> None:
        max_id = (
            await session.exec(
                select(func.max(EventMessage.id)).where(
                    EventMessage.mid == mid, EventMessage.event_type == event_type
                )
            )
        ).one() or 0
        stmt = select(EventReadCursor).where(
            EventReadCursor.mid == mid, EventReadCursor.event_type == event_type
        )
        row = (await session.exec(stmt)).one_or_none()
        if row is None:
            row = EventReadCursor(mid=mid, event_type=event_type)
        row.last_read_id = max(row.last_read_id, int(max_id))
        row.last_read_at = datetime.now()
        session.add(row)


# ==================== 通用事件（点赞 / @ / 审核驳回 / 下架 / 举报结果）====================


class GenericEvent(BaseEvent):
    """「非评论」类事件的通用处理器。

    点赞 / @提及 / 审核驳回 / 举报下架 / 举报成立（未通过）等都不涉及评论层级关系，
    直接走 ``_generic_content``：资源类型由 source_type 推导，正文取事件自身 content。
    """

    async def build_msgfeed_content(
        self, ctx: MsgfeedBuildContext, latest: EventMessage, rows: list[EventMessage]
    ) -> EventMsgfeedContent:
        return await self._generic_content(ctx, latest)


class LikeEvent(GenericEvent):
    """点赞事件。"""

    event_type = EventTypeEnum.LIKE


class AtEvent(GenericEvent):
    """@提及事件。"""

    event_type = EventTypeEnum.AT


class AuditRejectEvent(GenericEvent):
    """内容审核驳回事件（通知作者）。"""

    event_type = EventTypeEnum.AUDIT_REJECT


class HideEvent(GenericEvent):
    """内容因举报被管理员下架事件（通知资源作者）。"""

    event_type = EventTypeEnum.HIDE


class ReportRejectEvent(GenericEvent):
    """举报未通过审核事件（通知举报人）。"""

    event_type = EventTypeEnum.REPORT_REJECT


class ReportResolvedEvent(GenericEvent):
    """举报成立已处理事件（通知举报人）。"""

    event_type = EventTypeEnum.REPORT_RESOLVED


# ==================== 评论回复事件（需回捞评论层级关系 / 正文）====================


class ReplyEvent(BaseEvent):
    """回复事件：msgfeed 需要回捞评论层级关系 / 正文 / 删除态。"""

    event_type = EventTypeEnum.REPLY

    async def build_msgfeed_content(
        self, ctx: MsgfeedBuildContext, latest: EventMessage, rows: list[EventMessage]
    ) -> EventMsgfeedContent:
        biz_id = latest.biz_id or ""
        stype = latest.source_type
        idx = (
            ctx.comment_index.get(int(biz_id))
            if biz_id.isdigit()
            else None
        )

        resource_type = _SOURCE_TYPE_TO_BIZ_TYPE.get(
            stype, InteractionBizTypeEnum.DYNAMIC
        ).value
        resource_id = ""
        root_id = ""
        source_id = biz_id  # 当前评论 / 消息自身唯一 ID
        target_id = ""
        source_content = ""
        target_content = ""
        # 触发评论是否处于「非正常状态」（被删 / 未过审 / 驳回 / 下架 / 待审）：
        # 为 True 时正文不回捞，前端展示「该评论已被删除」占位（对齐知乎）。
        comment_deleted = False
        if idx is not None:
            if idx.state is not CommentStateEnum.NORMAL:
                comment_deleted = True
                resource_id = str(idx.oid)
            else:
                root_pk = idx.root or 0
                parent_pk = idx.parent or 0
                target_pk = parent_pk if parent_pk else root_pk
                resource_id = str(idx.oid)
                # 一级评论（root == 自身）时 root_id 等于 source_id
                root_id = str(root_pk) if root_pk else source_id
                # 直接回复目标；一级评论无目标则为空串
                target_id = str(target_pk) if target_pk else ""
                source_content = ctx.comment_content.get(int(biz_id), "")
                # target 对应评论正文（楼中楼 / 根评论均展示，用于通知上下文）
                if target_id and target_id.isdigit():
                    target_content = ctx.comment_content.get(int(target_id), "")
        else:
            # 非评论回复场景（点赞 / @ / 动态类）：resource_id 取 biz_id（资源 / 实体 id）
            resource_id = biz_id if biz_id.isdigit() else ""
            # 回复类事件拿不到 CommentIndex（评论被物理删除 / 历史行 biz_id 写成动态 oid）：
            # 无法回捞正文，同样标记占位，不再依赖事件表 content 兜底。
            comment_deleted = True

        title, image = await _resolve_source_meta(
            ctx.session, stype, latest.source_id, latest.biz_id, ctx.dyn_cache
        )

        return EventMsgfeedContent(
            item_id=latest.id or 0,
            type=int(self.event_type),
            business=stype.value if stype else 0,
            resource_type=resource_type,
            resource_id=resource_id,
            root_id=root_id,
            source_id=source_id,
            target_id=target_id,
            title=title,
            desc=latest.content or "",
            image=image,
            source_content=source_content,
            target_content=target_content,
            comment_deleted=comment_deleted,
            ctime=int(latest.created_at.timestamp()) if latest.created_at else 0,
        )


# ==================== 类型 → 处理器 注册表 ====================


EVENT_REGISTRY: dict[EventTypeEnum, type[BaseEvent]] = {
    EventTypeEnum.LIKE: LikeEvent,
    EventTypeEnum.REPLY: ReplyEvent,
    EventTypeEnum.AT: AtEvent,
    EventTypeEnum.AUDIT_REJECT: AuditRejectEvent,
    EventTypeEnum.HIDE: HideEvent,
    EventTypeEnum.REPORT_REJECT: ReportRejectEvent,
    EventTypeEnum.REPORT_RESOLVED: ReportResolvedEvent,
}


__all__ = [
    "BaseEvent",
    "GenericEvent",
    "LikeEvent",
    "ReplyEvent",
    "AtEvent",
    "AuditRejectEvent",
    "HideEvent",
    "ReportRejectEvent",
    "ReportResolvedEvent",
    "MsgfeedBuildContext",
    "EVENT_REGISTRY",
    "build_dedup_key",
]
