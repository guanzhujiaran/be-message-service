"""事件处理器虚基类 + 公共读 / 写路径逻辑。

2.50.0 起从「静态方法工具类」重构为**面向对象**的事件处理器：

- 每种 ``InteractionActionTypeEnum`` 对应一个处理器类（``LikeEvent`` / ``ReplyEvent`` / ...），
  统一继承自虚基类 ``BaseEvent``；
- 写路径（上报）与跨类型的读路径（聚合 / 明细 / msgfeed / 已读 / 计数）的公共逻辑下沉到
  ``BaseEvent``，**只有「按类型差异化」的逻辑（主要是 msgfeed 内容体构建）被抽成抽象方法
  ``build_msgfeed_content``**，由子类实现；
- 业务方不再直接调 ``EventService``，而是 ``BaseEvent.from_req(req).report(session)``，
  或 ``ReplyEvent(mid=..., ...).report(session)`` 这样按对象操作，类型含义一目了然。

设计要点（沿用旧 ``EventService`` 的契约）：

- **上报（write）**：先过消息设置闸门 → 自赞过滤 → 黑名单静默 → dedup_key 幂等 → 落库；
- **聚合读（read）**：按 ``source_type + source_id`` 分组，把「12 人赞了同一条动态」
  收敛成一张卡片；
- **已读管理**：支持按 id / 类型 / 聚合分组三种粒度。

幂等：``dedup_key``（唯一索引）由 ``mid:event_type:actor_mid:source_type:source_id:biz_id``
摘要而来。MQ 重投、前端重试、爬虫重复扫描都会被数据库直接拦掉。
"""
from __future__ import annotations

import hashlib
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import ClassVar

from bili_common.models import InteractionActionTypeEnum, InteractionBizTypeEnum
from loguru import logger
from sqlalchemy import case, func, tuple_
from sqlalchemy.exc import IntegrityError
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.biz_type import source_type_to_biz_type
from app.models.db import (
    CommentContent,
    CommentIndex,
    EventMessage,
    EventReadCursor,
    UserFollow,
)
from app.models.enums import CommentStateEnum, FollowStatusEnum
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
from app.services.message.insite.setting import SettingService
from app.services.user.account import PptrUser
from app.services.user.follow import FollowService

from .constants import (
    _ACTOR_SCAN_LIMIT,
    _MAX_ACTORS_PER_GROUP,
    _MAX_USERS_PER_ITEM,
)
from .registry import EVENT_REGISTRY

# ==================== 事件资源快照批量回捞（计划书 §5.11 / C20）====================


async def _load_event_resource_snapshots(
    session, metas
) -> "dict[tuple[int, str], InteractionResource]":
    """按 ``(resource_type, resource_id, rpid)`` 批量回捞资源快照，每类资源一次调用。

    metas: 可迭代的 ``(resource_type:int, resource_id:str, rpid:str|None)``。
    返回 ``{(resource_type, resource_id): InteractionResource}``；未实现资源类 / 未知类型
    返回 ``exists=False`` 的空占位（前端展示「资源已删除 / 不存在」且跳过跳转）。
    """
    from app.models.schemas.interaction import InteractionResource
    from app.services.interaction_actions.base_biz import get_biz_class

    by_type: dict[int, list[str]] = {}
    for rt, rid, _ in metas:
        if rid:
            by_type.setdefault(rt, []).append(rid)
    out: dict[tuple[int, str], InteractionResource] = {}
    for rt, ids in by_type.items():
        rpid_map = {
            int(i): rpid
            for (t, i, rpid) in metas
            if t == rt and str(i).isdigit() and rpid
        }
        try:
            biz_cls = get_biz_class(rt)
        except ValueError:
            biz_cls = None
        snaps: dict[int, InteractionResource] = {}
        if biz_cls is not None:
            snaps = await biz_cls.batch_get_resources(
                session, [int(i) for i in ids if str(i).isdigit()], rpid_map=rpid_map
            )
        for i in ids:
            out[(rt, i)] = snaps.get(int(i)) or InteractionResource(
                bizType=InteractionBizTypeEnum.DYNAMIC,
                bizId=int(i) if str(i).isdigit() else 0,
                exists=False,
            )
    return out


async def _resolve_event_identities(
    session, rows, comment_index: "dict[int, CommentIndex] | None" = None
) -> "dict[int, tuple[int, str, str]]":
    """把一批 ``EventMessage`` 解析为跳转身份 ``(resource_type, resource_id, rpid)``。

    评论锚定事件按 ``biz_id=rpid`` 经 ``CommentIndex`` 取顶层 ``resource_type``
    （DYNAMIC/LOTTERY）+ ``resource_id``（oid），``rpid`` 作楼层锚点；其余事件
    ``resource_type`` = ``source_type`` 对应 bizType，``resource_id`` = ``biz_id``。
    """
    from app.models.db import CommentIndex

    out: dict[int, tuple[int, str, str]] = {}
    need: set[int] = set()
    for r in rows:
        if (
            is_comment_anchored(r.event_type, r.source_type)
            and r.biz_id
            and str(r.biz_id).isdigit()
        ):
            if comment_index is None or int(r.biz_id) not in comment_index:
                need.add(int(r.biz_id))
    extra: dict[int, CommentIndex] = {}
    if need:
        res = await session.exec(
            select(CommentIndex).where(CommentIndex.rpid.in_(need))
        )
        extra = {row.rpid: row for row in res}
    cidx: dict[int, CommentIndex] = {**(comment_index or {}), **extra}
    for r in rows:
        biz = source_type_to_biz_type(r.source_type)
        rt = biz.value if biz else 0
        rid = r.biz_id or (r.source_id or "")
        rpid = ""
        if (
            is_comment_anchored(r.event_type, r.source_type)
            and r.biz_id
            and str(r.biz_id).isdigit()
        ):
            idx = cidx.get(int(r.biz_id))
            if idx is not None:
                own = idx.type
                rt = own.value if isinstance(own, InteractionBizTypeEnum) else int(own)
                rid = str(idx.oid)
                rpid = r.biz_id
        out[r.id] = (rt, rid, rpid)
    return out


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


@dataclass
class CommentLocate:
    """事件在资源上的定位结果（回复 / @ / 点赞 / 系统处置统一复用）。

    - ``resource_type + resource_id``：定位**跳转目标**。评论场景为「评论所属顶层
      资源」的 类型 + oid，与前端 ``openEventDetail`` 的跳转判定同源；
    - ``root_id / source_id / target_id``：定位**评论楼层**（根评论 / 触发评论 /
      被回复评论的 rpid），前端据此深链定位到具体楼层；
    - ``source_content / target_content``：对应楼层的评论正文（读取时实时回捞）；
    - ``comment_deleted``：触发评论已不可见（删 / 未过审 / 驳回 / 下架），
      正文不回捞，前端展示「该评论已被删除」占位。
    """

    resource_type: int
    resource_id: str = ""
    root_id: str = ""
    source_id: str = ""
    target_id: str = ""
    source_content: str = ""
    target_content: str = ""
    comment_deleted: bool = False


def is_comment_anchored(
    event_type: InteractionActionTypeEnum,
    source_type: InteractionBizTypeEnum | int | None,
) -> bool:
    """该事件是否「评论锚定」：``biz_id`` 是评论 rpid，需按 ``CommentIndex`` 回捞楼层关系。

    - ``REPLY``：``biz_id`` **恒为评论 rpid**（不随 source_type 变成 oid）；
    - 其余类型（@ / 点赞 / 审核 / 举报处置）：仅当 ``source_type`` 为 COMMENT 时
      biz_id 才是 rpid；对动态 / 抽奖本身发起时 biz_id 是资源 oid，
      不能当 rpid 查 CommentIndex。
    """
    if event_type is InteractionActionTypeEnum.REPLY:
        return True
    return source_type_to_biz_type(source_type) is InteractionBizTypeEnum.COMMENT


def _replace_at_mentions(message: str, at_nickname_map: dict[int, str]) -> str:
    """把评论正文里的 `@{mid}` 占位符替换为 `@昵称`（对齐 comment_read）。

    - 只替换能命中 `at_nickname_map`（mid → 昵称）的占位符；
    - 命中不到的（mid 不存在 / 用户已删）原样保留，由前端兜底清理，
      避免把「@ 关系」丢成一个裸 `@` 或产生错误的人名。
    """
    if not message or not at_nickname_map:
        return message

    def _sub(match: re.Match[str]) -> str:
        mid = int(match.group(1))
        nickname = at_nickname_map.get(mid)
        if not nickname:
            return match.group(0)
        return f"@{nickname}"

    return re.sub(r"@\{(\d{1,19})\}", _sub, message)


def build_dedup_key(
    mid: int,
    event_type: InteractionActionTypeEnum,
    actor_mid: int,
    source_type: InteractionBizTypeEnum,
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


async def report_event_weakly(req: EventReportReq) -> None:
    """弱依赖事件上报：独立会话投递，失败不影响主事务。

    点赞 / 回复 / @ / 审核驳回 / 举报下架 / 举报结果等互动通知统一走这里，
    避免每个调用点都重复 ``async with new_session()`` + ``try/except`` 的样板。
    核心执行仍是对象式 ``BaseEvent.from_req(req).report(session)``。
    """
    from app.core.database import new_session

    try:
        async with new_session() as ns:
            await BaseEvent.from_req(req).report(ns)
    except Exception:  # noqa: BLE001
        logger.warning(
            f"事件上报失败（弱依赖，已忽略）: event_type={req.event_type} mid={req.mid}",
            exc_info=True,
        )


# ==================== 虚基类 ====================


class BaseEvent(ABC):
    """事件处理器的虚基类。

    子类**必须**声明类属性 ``event_type``（对应 ``InteractionActionTypeEnum`` 的一个成员），
    并（直接或通过 ``GenericEvent``）实现抽象方法 ``build_msgfeed_content``。

    设计分工：

    - 写路径 ``report``、跨类型读路径（``aggregate`` / ``list_detail`` /
      ``list_msgfeed`` / ``mark_read`` / ``delete`` / ``count_unread`` /
      ``count_unread_by_type``）是**与类型无关**的公共逻辑，由本基类直接提供；
    - 内容体构建同样**只有一份**：``_generic_content`` 按 :func:`is_comment_anchored`
      判断事件是否锚定在评论上，锚定的走 :meth:`_locate_comment` 回捞楼层关系 /
      正文（回复 / 评论 @ / 评论点赞 / 评论处置），否则走 :meth:`_plain_locate`
      只给资源 id；各类型处理器只需声明 ``event_type`` 与投递语义。
    """

    # 子类必须覆盖：该处理器对应的事件类型（DB 落地值，见 bili-common 的枚举）
    event_type: ClassVar[InteractionActionTypeEnum]

    # 投递语义（原挂在底层枚举上的行为元数据，已下沉到本处理器类）：
    # blocked_silent：触发者与接收方存在任一向黑名单关系时是否静默、不投递提醒
    #   （@ / 回复这类「点对点打扰」才为 True；点赞 / 系统通知为 False）
    blocked_silent: ClassVar[bool] = False
    # setting_gate：对应的用户消息设置闸门字段（如 recv_like）；None 表示
    #   系统侧通知（无用户开关，恒投递）
    setting_gate: ClassVar[str | None] = None

    def __init__(
        self,
        *,
        mid: int,
        actor_mid: int,
        source_type: InteractionBizTypeEnum,
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
    def from_req(cls, req: EventReportReq) -> BaseEvent:
        """从上报请求构造对应类型的事件处理器对象（最常用的入口）。"""
        handler_cls = _resolve_handler_cls(req.event_type)
        return handler_cls(
            mid=req.mid,
            actor_mid=req.actor_mid,
            source_type=req.source_type,
            source_id=req.source_id,
            biz_id=req.biz_id,
            content=req.content,
        )

    @classmethod
    def _for_type(
        cls, event_type: InteractionActionTypeEnum, source_type: InteractionBizTypeEnum
    ) -> BaseEvent:
        """读路径：按分组里的 event_type 取出对应处理器实例（mid 占位，仅用于构建内容）。

        ``source_type`` 取自分组的真实来源类型（不再用占位默认值），供内容构建正确回捞资源。
        """
        handler_cls = _resolve_handler_cls(event_type)
        return handler_cls(mid=0, actor_mid=0, source_type=source_type, source_id="")

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

        顺序：消息设置闸门 → 自赞过滤 → 黑名单静默 → dedup_key 幂等 → 落库。
        """
        # 闸门一：用户是否愿意接收这类提醒
        accepted = await SettingService.accept_event(session, self.mid, self.setting_gate)
        if not accepted:
            logger.debug(f"用户 {self.mid} 已关闭 {self.event_type} 提醒，跳过")
            return EventReportResp(accepted=False, duplicated=False)

        # 闸门二：不给自己发提醒
        if self.mid == self.actor_mid:
            return EventReportResp(accepted=False, duplicated=False)

        # 闸门三：黑名单静默（2.50.0）——@ / 回复不打扰黑名单用户。
        # `blocked_silent` 由 handler 类属性声明（如 ReplyEvent / AtEvent），
        # 不再依赖底层枚举元数据。@ 本身仍然允许（动态 AT 节点 / 评论 @ 关系照常落库与渲染），
        # 这里只拦「提醒投递」这一步；双向任一向拉黑即静默。
        if self.blocked_silent:
            if await FollowService.is_blocked_relation(session, self.mid, self.actor_mid):
                logger.debug(
                    f"用户 {self.mid} 与 {self.actor_mid} 存在黑名单关系，"
                    f"跳过 {self.event_type} 提醒"
                )
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
        （用于构建触发者头像列表等组内上下文）。

        内容体本身由 ``_generic_content`` 统一构建，子类默认无需重写。
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

    def _plain_locate(self, latest: EventMessage) -> CommentLocate:
        """非评论锚定事件的定位：``biz_id`` 即资源 id，无楼层关系。

        典型：对动态 / 抽奖本身点赞或 @（``source_type`` 为 DYNAMIC / LOTTERY）。
        """
        biz_type = source_type_to_biz_type(latest.source_type)
        if biz_type is None:
            raise ValueError(
                f"事件来源类型 {latest.source_type} 无对应的业务资源类型，无法构建 msgfeed"
            )
        biz_id = latest.biz_id or ""
        return CommentLocate(
            resource_type=biz_type.value,
            resource_id=biz_id if biz_id.isdigit() else "",
        )

    def _locate_comment(
        self, ctx: MsgfeedBuildContext, latest: EventMessage
    ) -> CommentLocate:
        """回捞「评论锚定」事件的楼层关系与正文（读取时实时回捞，不冗余存储）。

        三种结果：

        1. 命中 ``CommentIndex`` 且状态正常 → 展开三层楼层关系与正文；
        2. 命中但状态非正常（删 / 未过审 / 驳回 / 下架 / 待审）→ 只给 ``resource_id``，
           正文不回捞并标记 ``comment_deleted``；
        3. 未命中（评论被物理删除 / 历史行 biz_id 写成 oid）→ 同样标记删除态，
           ``resource_id`` 回落 biz_id。

        ``resource_type`` 表达「该事件跳过去的实际原资源类型」（与 ``resource_id``
        配对），与 ``business``（source_type）「通知文案中的被互动对象」是两个独立
        维度：评论 @ / 评论点赞 / 楼中楼的 source_type=COMMENT，但跳转目标是评论
        所属顶层资源（DYNAMIC/LOTTERY），故取 ``idx.type``；一级评论（source_type
        已是 DYNAMIC/LOTTERY）直接跟随 source_type。
        """
        biz_id = latest.biz_id or ""
        biz_type = source_type_to_biz_type(latest.source_type)
        if biz_type is None:
            raise ValueError(
                f"事件来源类型 {latest.source_type} 无对应的业务资源类型，无法构建 msgfeed"
            )
        idx = ctx.comment_index.get(int(biz_id)) if biz_id.isdigit() else None

        if idx is not None and biz_type is InteractionBizTypeEnum.COMMENT:
            own_type = idx.type
            resource_type = (
                own_type.value
                if isinstance(own_type, InteractionBizTypeEnum)
                else int(own_type)
            )
        else:
            resource_type = biz_type.value

        if idx is None:
            return CommentLocate(
                resource_type=resource_type,
                resource_id=biz_id if biz_id.isdigit() else "",
                comment_deleted=True,
            )
        if idx.state is not CommentStateEnum.NORMAL:
            return CommentLocate(
                resource_type=resource_type,
                resource_id=str(idx.oid),
                comment_deleted=True,
            )

        root_pk = idx.root or 0
        parent_pk = idx.parent or 0
        target_pk = parent_pk if parent_pk else root_pk
        # 触发评论自身 rpid（一级评论时 root_id 与之相同，target_id 为空）
        source_id = biz_id
        target_id = str(target_pk) if target_pk else ""
        return CommentLocate(
            resource_type=resource_type,
            resource_id=str(idx.oid),
            root_id=str(root_pk) if root_pk else source_id,
            source_id=source_id,
            target_id=target_id,
            source_content=ctx.comment_content.get(int(biz_id), ""),
            target_content=(
                ctx.comment_content.get(int(target_id), "")
                if target_id and target_id.isdigit()
                else ""
            ),
        )

    async def _generic_content(
        self, ctx: MsgfeedBuildContext, latest: EventMessage
    ) -> EventMsgfeedContent:
        """统一的 msgfeed 内容体构建（点赞 / @ / 回复 / 审核 / 举报全部复用）。

        评论锚定事件（见 :func:`is_comment_anchored`）走 :meth:`_locate_comment`
        回捞楼层关系与正文；其余事件走 :meth:`_plain_locate`，只给资源 id。
        """
        locate = (
            self._locate_comment(ctx, latest)
            if is_comment_anchored(self.event_type, latest.source_type)
            else self._plain_locate(latest)
        )
        return EventMsgfeedContent(
            item_id=latest.id or 0,
            type=int(self.event_type),
            business=latest.source_type.value if latest.source_type else 0,
            resource_type=locate.resource_type,
            resource_id=locate.resource_id,
            root_id=locate.root_id,
            source_id=locate.source_id,
            target_id=locate.target_id,
            # title / image / jump_target 由 list_msgfeed 末尾的批量资源快照统一装配
            title="",
            desc=latest.content or "",
            image="",
            source_content=locate.source_content,
            target_content=locate.target_content,
            comment_deleted=locate.comment_deleted,
            ctime=int(latest.created_at.timestamp()) if latest.created_at else 0,
        )

    # ==================== 跨类型读路径（集合操作，保留为类方法）====================

    @classmethod
    async def aggregate(
        cls,
        session: AsyncSession,
        mid: int,
        event_type: InteractionActionTypeEnum | None = None,
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
        user_map = await PptrUser.get_many(actor_mids)

        # 资源身份（resource_type, resource_id, rpid）批量解析 + 快照回捞
        _latest_rows = [
            bucket[(etype, stype, sid)][0]
            for (etype, stype, sid, _c, _u, _l) in groups
            if bucket.get((etype, stype, sid))
        ]
        _identities = await _resolve_event_identities(session, _latest_rows)
        _snapshots = await _load_event_resource_snapshots(
            session, [_identities[r.id] for r in _latest_rows]
        )

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
            title = image = ""
            jump_target = ""
            resource_deleted = False
            if latest is not None:
                _idt = _identities.get(latest.id)
                if _idt is not None:
                    snap = _snapshots.get((_idt[0], _idt[1]))
                    if snap is not None:
                        title = snap.title or ""
                        image = snap.cover or ""
                        resource_deleted = not snap.exists
                        jump_target = (snap.jumpTarget or "") if snap.exists else ""
            items.append(
                EventAggregateItem(
                    event_type=etype,
                    source_type=stype,
                    source_id=sid,
                    biz_id=latest.biz_id if latest else None,
                    title=title,
                    image=image,
                    jump_target=jump_target,
                    resource_deleted=resource_deleted,
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
        event_type: InteractionActionTypeEnum | None = None,
        source_type: InteractionBizTypeEnum | None = None,
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
        # 资源身份（resource_type, resource_id, rpid）批量解析 + 快照回捞
        _identities = await _resolve_event_identities(session, rows)
        _snapshots = await _load_event_resource_snapshots(
            session, [_identities[r.id] for r in rows]
        )
        items: list[EventItem] = []
        for r in rows:
            title = image = ""
            jump_target = ""
            resource_deleted = False
            _idt = _identities.get(r.id)
            if _idt is not None:
                snap = _snapshots.get((_idt[0], _idt[1]))
                if snap is not None:
                    title = snap.title or ""
                    image = snap.cover or ""
                    resource_deleted = not snap.exists
                    jump_target = (snap.jumpTarget or "") if snap.exists else ""
            items.append(
                EventItem(
                    id=r.id or 0,
                    event_type=r.event_type,
                    source_type=r.source_type,
                    source_id=r.source_id,
                    biz_id=r.biz_id,
                    title=title,
                    image=image,
                    jump_target=jump_target,
                    resource_deleted=resource_deleted,
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
        event_type: InteractionActionTypeEnum | None = None,
        cursor_id: int | None = None,
        page_size: int = 20,
        only_unread: bool = False,
    ) -> EventListResp:
        """按 source_type + source_id 聚合的 B 站式互动提醒列表。

        聚合分组键为 ``(event_type, source_type, source_id)``，每组的内容体
        交由 ``_for_type(event_type).build_msgfeed_content(...)`` 按类型构建。

        翻页设计（2.5x 修正）：分组以「组内最新事件 id」即 ``max(id)`` 排序定位页边界，
        因此**游标翻页、has_more、total_count 都必须作用在分组聚合后的 ``max(id)`` 上**，
        而不能把 ``id < cursor`` 当作事件行的过滤条件。

        原因：一组往往包含**多条事件**（同一个人对同一动态多次 @），若用 ``id < cursor``
        直接过滤事件行，一旦某组同时含「>= cursor 的 max(id)」与「< cursor 的旧事件」，
        整组就会被下一页的游标条件误剔除——导致翻页**丢组、总数对不上、提前 is_end**。
        修正后以 ``HAVING max(id) < cursor`` 定位，确保每组按最新事件被完整分配到唯一一页。
        """
        base_conditions = [EventMessage.mid == mid, EventMessage.is_deleted == False]
        if event_type is not None:
            base_conditions.append(EventMessage.event_type == event_type)
        if only_unread:
            base_conditions.append(EventMessage.is_read == False)

        group_cols = (
            EventMessage.event_type,
            EventMessage.source_type,
            EventMessage.source_id,
        )

        # ---- 分组聚合子查询：只在『事件行级』筛选上做 group by，游标用 HAVING 加在 max(id) 上 ----
        # （max(id) 是定位列：分组排序 / 翻页 / has_more / total_count 共用同一口径，保证自洽）
        unread_expr = func.sum(case((EventMessage.is_read == False, 1), else_=0))
        group_stmt = (
            select(
                *group_cols,
                func.count().label("cnt"),
                unread_expr.label("unread"),
                func.max(EventMessage.id).label("latest_id"),
            )
            .where(*base_conditions)
            .group_by(*group_cols)
            .order_by(func.max(EventMessage.id).desc())
        )
        # 游标翻页：取「组内最新事件 id < cursor」的后一页分组
        if cursor_id is not None:
            group_stmt = group_stmt.having(func.max(EventMessage.id) < cursor_id)
        groups = (await session.exec(group_stmt.limit(page_size))).all()

        # 是否存在更早的分组（决定本页 is_end）——同样按 max(id) 口径
        has_more = False
        if groups:
            tail_stmt = group_stmt.offset(page_size).limit(1)
            has_more = (await session.exec(tail_stmt)).one_or_none() is not None
        else:
            has_more = False

        if not groups:
            return EventListResp(
                latest=EventMsgfeedSection(
                    cursor=EventMsgfeedCursor(is_end=True, id=None, time=None), items=[]
                ),
                total=EventMsgfeedSection(
                    cursor=EventMsgfeedCursor(is_end=True, id=None, time=None), items=[]
                ),
                total_count=0,
                unread_count=await cls.count_unread(session, mid, event_type),
            )

        group_keys = [(g[0], g[1], g[2]) for g in groups]
        detail_stmt = (
            select(EventMessage)
            .where(
                *base_conditions,
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
        user_map = await PptrUser.get_many(actor_mids)
        dyn_cache: dict[int, object] = {}

        # ---- 评论关系 / 正文 / 点赞态 / 关注态，读取时实时回捞 ----
        # 只处理「评论锚定」分组的 biz_id（触发评论 rpid，见 is_comment_anchored）：
        # REPLY 恒为 rpid（不再随 source_type 变成动态 oid），因此 REPLY+DYNAMIC
        # （动态评论的回复）组合同样需要回捞；@ / 点赞 / 系统处置只有 source_type
        # 为 COMMENT 时 biz_id 才是 rpid（对动态本身点赞 / @ 时是 oid，不能当 rpid 查）。
        # 历史脏数据（biz_id 写成动态 oid）回捞不到时，由 _locate_comment 的
        # 兜底分支标记删除态，不影响其余字段。
        comment_biz_ids: set[str] = set()
        for etype, stype, sid, _cnt, _unread, _latest_id in groups:
            if not is_comment_anchored(etype, stype):
                continue
            for r in bucket.get((etype, stype, sid), []):
                if r.biz_id:
                    comment_biz_ids.add(r.biz_id)

        comment_biz_ints = [int(b) for b in comment_biz_ids if b.isdigit()]
        comment_index: dict[int, CommentIndex] = {}
        comment_content: dict[int, str] = {}
        if comment_biz_ints:
            idx_rows = (
                await session.exec(
                    select(CommentIndex).where(CommentIndex.rpid.in_(comment_biz_ints))
                )
            ).all()
            comment_index = {row.rpid: row for row in idx_rows}
            all_rpids = set(comment_biz_ints)
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
            # 回捞被 @ 用户的昵称，把正文里的 `@{mid}` 占位符替换为 `@昵称`
            # （对齐 comment_read：正文不落昵称快照，读取时按 at_mids 回查补全）。
            at_mids: set[int] = set()
            for row in content_rows:
                at_mids.update(row.at_mids or [])
            at_nickname_map: dict[int, str] = {}
            if at_mids:
                profiles = await PptrUser.get_many(at_mids)
                at_nickname_map = {
                    int(mid): (brief.uname or "").strip()
                    for mid, brief in profiles.items()
                    if (brief.uname or "").strip()
                }
            if at_nickname_map:
                comment_content = {
                    rpid: _replace_at_mentions(msg, at_nickname_map)
                    for rpid, msg in comment_content.items()
                }

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
            handler = cls._for_type(etype, stype)
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

        # ---- 资源快照批量回捞（按 bizType 分组，每类一次 batch_get_resources）----
        # 资源身份统一为 (resource_type, resource_id)，跳转目标由后端按 bizType 下发
        # route:{name}?{query}（见 §2.10 / §5.11 / C20）；资源不存在返回 exists=False，
        # 前端展示「资源已删除 / 不存在」且跳过跳转。
        # rpid（楼层锚点）仅评论锚定事件才有意义：其 source_id 才是评论 rpid；
        # 普通点赞/@（source_id 是动态/资源 id）不能当 rpid 写入跳转参数。
        _metas = []
        for item in total_items:
            c = item.item
            if not c.resource_id:
                continue
            rpid = None
            if is_comment_anchored(
                InteractionActionTypeEnum(c.type), InteractionBizTypeEnum(c.business)
            ):
                rpid = c.source_id or None
            _metas.append((c.resource_type, c.resource_id, rpid))
        _snapshots = await _load_event_resource_snapshots(session, _metas)
        for item in total_items:
            c = item.item
            snap = _snapshots.get((c.resource_type, c.resource_id))
            if snap is None:
                continue
            c.title = snap.title or ""
            c.image = snap.cover or ""
            c.resource_deleted = not snap.exists
            c.jump_target = (snap.jumpTarget or "") if snap.exists else ""

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

        # ---- 对账字段：聚合卡片总数 + 未读事件总数 ----
        # 列表按「来源实体」聚合，单页 items 长度 = min(page_size, 卡片数)，
        # 与「未读事件数」天然不等（一张卡片聚合 N 个用户的同类互动）。这两个字段给出
        # 真实总量，避免调用方把单页 20 张误判为「全部只有 20 条 / 已结束」。
        # 注意：total_count 统计「全部页的卡片总数」，不能带游标条件（cursor 只用于翻页）。
        total_cards = int(
            (
                await session.exec(
                    select(func.count()).select_from(
                        select(*group_cols)
                        .where(*base_conditions)
                        .group_by(*group_cols)
                        .subquery()
                    )
                )
            ).one()
            or 0
        )
        # 未读事件总数：与 GET /unread 的对应字段口径一致（mid + is_read + is_deleted + event_type）
        unread_count = await cls.count_unread(session, mid, event_type)

        return EventListResp(
            latest=EventMsgfeedSection(cursor=latest_cursor, items=total_items[:1]),
            total=EventMsgfeedSection(cursor=cursor, items=total_items),
            total_count=total_cards,
            unread_count=unread_count,
        )

    @classmethod
    async def mark_read(
        cls, session: AsyncSession, mid: int, req: EventReadReq
    ) -> EventReadResp:
        """标记已读，支持 id / 类型 / 聚合分组 / 时间戳四种粒度。

        - 传 ``event_ids`` → 精确已读；
        - 传 ``event_type`` → 该类型一键已读；
        - 再加 ``source_type + source_id`` → 只清掉某一张聚合卡片；
        - 传 ``read_before``（datetime）→ 把该时间戳（含）之前、归属当前用户的全部互动提醒
          标记为已读（用于「打开列表即自动已读」：前端在拉取列表后携带调用时刻调用，
          即可把本次请求之前的点赞 / 回复 / @ 消息全部置为已读）。
        """
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

        # 时间闸门：仅把 cutoff（含）之前的消息置为已读，之后的新消息保持未读。
        # cutoff 默认取服务端当前时间；若前端传入 read_before（UTC，带 Z），
        # 则换算到服务端本地时区并转为 naive，与 naive 的 created_at 对齐，避免时区错位。
        cutoff = req.read_before
        if cutoff is not None and cutoff.tzinfo is not None:
            cutoff = cutoff.astimezone().replace(tzinfo=None)
        if cutoff is None:
            cutoff = datetime.now()
        conditions.append(table.c.created_at <= cutoff)

        now = datetime.now()
        result = await session.exec(
            table.update().where(*conditions).values(  # type: ignore[call-overload]
                is_read=True, read_at=now, updated_at=now
            )
        )
        affected = int(getattr(result, "rowcount", 0) or 0)

        if req.event_type is not None and not req.event_ids:
            await cls._advance_cursor(session, mid, req.event_type, cutoff)

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
        cls,
        session: AsyncSession,
        mid: int,
        event_type: InteractionActionTypeEnum | None = None,
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
    async def count_unread_by_type(
        cls, session: AsyncSession, mid: int
    ) -> dict[int, int]:
        """一次查询拿到各类型未读数（前端红点）。

        键使用 ``int``（即 ``InteractionActionTypeEnum.value``）：调用方均以
        ``InteractionActionTypeEnum.LIKE.value`` 等整数取值查表，若用 ``str(etype)`` 作为键会
        与整数键不匹配，导致点赞 / 回复 / @ 未读数恒为 0。
        """
        stmt = (
            select(EventMessage.event_type, func.count())
            .where(
                EventMessage.mid == mid,
                EventMessage.is_read == False,
                EventMessage.is_deleted == False,
            )
            .group_by(EventMessage.event_type)
        )
        return {int(etype): int(cnt) for etype, cnt in (await session.exec(stmt)).all()}

    @classmethod
    async def _advance_cursor(
        cls,
        session: AsyncSession,
        mid: int,
        event_type: InteractionActionTypeEnum,
        read_before: datetime | None = None,
    ) -> None:
        max_id_stmt = select(func.max(EventMessage.id)).where(
            EventMessage.mid == mid, EventMessage.event_type == event_type
        )
        # read_before 限定下，已读水位只抬到该时间戳（含）之前的最大 id
        if read_before is not None:
            max_id_stmt = max_id_stmt.where(EventMessage.created_at <= read_before)
        max_id = (await session.exec(max_id_stmt)).one() or 0
        stmt = select(EventReadCursor).where(
            EventReadCursor.mid == mid, EventReadCursor.event_type == event_type
        )
        row = (await session.exec(stmt)).one_or_none()
        if row is None:
            row = EventReadCursor(mid=mid, event_type=event_type)
        row.last_read_id = max(row.last_read_id, int(max_id))
        row.last_read_at = datetime.now()
        session.add(row)


class GenericEvent(BaseEvent):
    """全部事件类型的通用处理器（点赞 / @ / 回复 / 审核 / 举报处置）。

    msgfeed 内容体统一走 ``_generic_content``：

    - **评论锚定**事件（回复，或 source_type=COMMENT 的 @ / 点赞 / 处置）
      经 ``_locate_comment`` 回捞楼层关系与正文，可深链定位到具体评论；
    - 其余事件（对动态 / 抽奖本身的点赞 / @ / 处置）走 ``_plain_locate``，
      资源类型由 source_type 推导，正文取事件自身 content。
    """

    async def build_msgfeed_content(
        self,
        ctx: MsgfeedBuildContext,
        latest: EventMessage,
        rows: list[EventMessage],
    ) -> EventMsgfeedContent:
        return await self._generic_content(ctx, latest)


def _resolve_handler_cls(event_type: InteractionActionTypeEnum) -> type[BaseEvent]:
    """按事件类型取处理器类；未登记的类型回落到 GenericEvent。"""
    spec = EVENT_REGISTRY.get(event_type)
    return spec.handler_cls if spec is not None else GenericEvent
