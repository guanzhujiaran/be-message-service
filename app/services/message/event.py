"""事件提醒服务（点赞 / 回复 / @提及）。

三条主线：

1. **上报（write）**：先过消息设置这道闸门 → 计算幂等键 → 落库 →
   按用户活跃度决定实时推送还是进批量队列。
2. **聚合读（read）**：按 `source_type + source_id` 分组，
   把「12 个人赞了同一条动态」收敛成一张卡片，避免消息中心被同质消息刷屏。
   分组走 `idx_event_group` 联合索引，不需要额外的聚合表。
3. **已读管理**：支持按 id、按类型、按聚合分组三种粒度。

幂等：`dedup_key`（唯一索引）由 `mid:event_type:actor_mid:source_type:source_id:biz_id`
摘要而来。MQ 重投、前端重试、爬虫重复扫描都会被数据库直接拦掉，
上层拿到 `duplicated=True` 即可，无需自己去重。
"""

import contextlib
import hashlib
from datetime import datetime

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
from app.models.db.moment import TMoment
from app.models.enums import EventTypeEnum, FollowStatusEnum, SourceTypeEnum
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


async def _resolve_source_meta(
    session: AsyncSession,
    source_type: SourceTypeEnum | None,
    source_id: str | None,
    biz_id: str | None,
    dyn_cache: dict[int, object] | None = None,
) -> tuple[str, str, str]:
    """按 source_type + source_id 实时回捞原资源的标题 / 封面 / 跳转链接。

    - DYNAMIC：直接读 TMoment（与事件表同库）；
    - COMMENT：先按 biz_id 取评论索引拿到 oid，再读 TMoment；
    - LOTTERY：走 RPA RPC 取详情（弱依赖，失败返回空）；
    - 其余类型（VIDEO / ARTICLE / OTHER）本地无原资源，返回空串（由前端按 resource_id 兜底）。

    仅依赖 id，不在事件表冗余存储快照，省空间也更不易过期。
    """
    st = source_type
    if isinstance(st, int):
        with contextlib.suppress(Exception):
            st = SourceTypeEnum(st)

    if st is SourceTypeEnum.DYNAMIC and source_id:
        dyn = await _load_dynamic(session, source_id, dyn_cache)
        if dyn is not None:
            return (
                dyn.contentText or "",
                _first_pic(dyn.contentJson) or "",
                f"/detail/{source_id}",
            )

    if st is SourceTypeEnum.COMMENT and biz_id:
        if biz_id.isdigit():
            idx = await session.get(CommentIndex, int(biz_id))
            if idx is not None and idx.oid:
                dyn = await _load_dynamic(session, str(idx.oid), dyn_cache)
                if dyn is not None:
                    return (
                        dyn.contentText or "",
                        _first_pic(dyn.contentJson) or "",
                        f"/detail/{idx.oid}",
                    )
        return ("", "", "")

    if st is SourceTypeEnum.LOTTERY and source_id:
        try:
            from app.services.infrastructure.rpa_rpc import rpa_rpc_client

            detail = await rpa_rpc_client.get_resource_detail(
                InteractionBizTypeEnum.LOTTERY.to_text(), source_id
            )
            if detail is not None:
                name = getattr(detail, "name", None) or ""
                cover = getattr(detail, "cover", None) or ""
                jump = getattr(detail, "jumpUrl", None) or ""
                return (str(name), str(cover), str(jump))
        except Exception:  # noqa: BLE001
            logger.debug("回捞 lottery 资源详情失败（弱依赖，忽略）", exc_info=True)
        return ("", "", "")

    return ("", "", "")


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
    """计算事件幂等键。

    未传 `biz_id` 时，同一个人对同一实体的同类行为只会记一条
    （反复点赞取消点赞不会刷屏）；传了 `biz_id`（如评论 id）则按业务实体区分，
    同一个人的多条回复各记一条。
    """
    raw = f"{mid}:{event_type}:{actor_mid}:{source_type}:{source_id}:{biz_id or ''}"
    return hashlib.md5(raw.encode("utf-8")).hexdigest()


class EventService:
    """事件提醒的上报、聚合查询与已读管理。"""

    # ==================== 上报 ====================

    @staticmethod
    async def report(session: AsyncSession, req: EventReportReq) -> EventReportResp:
        """上报一条用户行为事件。

        事件提醒是站内信的一种：落库即送达，由接收方通过 /list / msg_feed 轮询读取，
        不再做任何第三方渠道推送或「实时 / 批量」分流。
        """
        # 闸门一：用户是否愿意接收这类提醒
        accepted = await SettingService.accept_event(session, req.mid, req.event_type)
        if not accepted:
            logger.debug(f"用户 {req.mid} 已关闭 {req.event_type} 提醒，跳过")
            return EventReportResp(accepted=False, duplicated=False)

        # 闸门二：不给自己发提醒
        if req.mid == req.actor_mid:
            return EventReportResp(accepted=False, duplicated=False)

        dedup_key = build_dedup_key(
            req.mid,
            req.event_type,
            req.actor_mid,
            req.source_type,
            req.source_id,
            req.biz_id,
        )

        row = EventMessage(
            mid=req.mid,
            event_type=req.event_type,
            source_type=req.source_type,
            source_id=req.source_id,
            biz_id=req.biz_id,
            actor_mid=req.actor_mid,
            content=req.content,
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

    # ==================== 聚合查询 ====================

    @staticmethod
    async def aggregate(
        session: AsyncSession,
        mid: int,
        event_type: EventTypeEnum | None = None,
        page_num: int = 1,
        page_size: int = 20,
        only_unread: bool = False,
    ) -> tuple[list[EventAggregateItem], int]:
        """按 source_type + source_id 聚合展示。

        SQL 层只做分组统计（COUNT / SUM / MAX），拿到本页的分组键之后
        再回捞一次明细补齐「最新内容」与「头像列表」，
        避免在 GROUP BY 里做复杂的窗口函数，MySQL 8 以下也能跑。
        """
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

        # 分组总数
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

        # 回捞明细：一次查完本页所有分组的事件，在内存里取每组最新的若干条
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

        # 触发者用户信息（昵称 / 头像 / 粉丝数），读取时按 mid 回查用户服务（不冗余存储 name / avatar）
        actor_mids: set[int] = {r.actor_mid for r in details}
        user_map = await PptrUserService.get_many(actor_mids)

        items: list[EventAggregateItem] = []
        for etype, stype, sid, cnt, unread, latest_id in groups:
            rows = bucket.get((etype, stype, sid), [])
            latest = rows[0] if rows else None
            # 头像去重：同一个人多条事件只展示一次；仅保留 mid，昵称 / 头像读取时回查
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
            title, image, uri = await _resolve_source_meta(
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
                    uri=uri,
                    count=int(cnt or 0),
                    unread_count=int(unread or 0),
                    actors=actors,
                    latest_event_id=int(latest_id or 0),
                    latest_desc=latest.content if latest else None,
                    latest_at=latest.created_at if latest else None,
                )
            )
        return items, total

    @staticmethod
    async def list_detail(
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
            title, image, uri = await _resolve_source_meta(
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
                    uri=uri,
                    actor_mid=r.actor_mid,
                    desc=r.content,
                    is_read=r.is_read,
                    created_at=r.created_at,
                )
            )
        return items, total

    # ==================== B 站式 msgfeed 聚合列表 ====================

    @staticmethod
    async def list_msgfeed(
        session: AsyncSession,
        mid: int,
        event_type: EventTypeEnum | None = None,
        cursor_id: int | None = None,
        page_size: int = 20,
        only_unread: bool = False,
    ) -> EventListResp:
        """按 source_type + source_id 聚合的 B 站式互动提醒列表。

        对齐 `x/msgfeed/like` 结构：
        - `latest`：最新一条聚合记录（无分页），用于顶部"最近提醒"；
        - `total`：完整分页列表，`cursor.id` 为上一页末条分组最大事件 id，
          用于下一页翻页；`cursor.is_end` 表示是否已到末尾。

        聚合分组键为 `(event_type, source_type, source_id)`，
        每组返回：完整触发者 `users[]`（去重，按触发时间倒序）、
        内容实体 `item`（取组内最新一条）、总人数 `counts`。
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

        # 每组分组的统计：总数 / 最新事件 id
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
                latest=EventMsgfeedSection(items=[]),
                total=EventMsgfeedSection(
                    cursor=EventMsgfeedCursor(is_end=True, id=None, time=None),
                    items=[],
                ),
            )

        # 回捞本页所有分组的明细，内存里按组聚合
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

        # 批量补齐触发者用户信息（昵称/头像/粉丝数），避免事件表里 actor_* 为空
        actor_mids: set[int] = set()
        for rows in bucket.values():
            for r in rows:
                actor_mids.add(r.actor_mid)
        user_map = await PptrUserService.get_many(actor_mids)
        # 动态详情缓存：避免 msgfeed 一页内对同一条动态重复查库
        dyn_cache: dict[int, object] = {}

        # ---- Phase L2：评论关系 / 正文 / 点赞态 / 关注态，读取时实时回捞 ----
        # 只处理 REPLY 类型：收集本页全部 biz_id（触发评论 rpid），
        # 一次 `IN` 查询回捞评论索引/正文/互动，避免循环内发查询（SQL 次数与页大小无关）。
        # 注意：DYNAMIC 来源的 REPLY 事件 biz_id 记录的是动态 id（非评论 rpid），
        # 不能当作评论 rpid 回捞，故跳过该来源的 biz_id 收集。
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
            # 正文：触发评论 + 根评论 + 被回复评论（一次查完）
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

        # 关注态：接收者是否关注了触发者（一次查完本页全部触发者）
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

        def _user_brief(actor_mid: int, fallback_name, fallback_avatar) -> EventUserBrief:
            info = user_map.get(int(actor_mid))
            return EventUserBrief(
                mid=actor_mid,
                nickname=(info.uname if info else None) or fallback_name,
                avatar=(info.avatar if info else None) or fallback_avatar,
                fans=int(getattr(info, "follower_count", 0) or 0) if info else 0,
                follow=actor_mid in follow_targets,
            )

        async def _to_item(
            etype: int,
            stype: SourceTypeEnum | None,
            rows: list[EventMessage],
            cnt: int,
            session: AsyncSession,
            dyn_cache: dict[int, object],
        ) -> EventMsgfeedItem:
            """把一组明细收敛成一条 msgfeed 聚合条目。"""
            latest = rows[0]
            users: list[EventUserBrief] = []
            seen: set[int] = set()
            for r in rows:
                if r.actor_mid in seen:
                    continue
                seen.add(r.actor_mid)
                users.append(
                    _user_brief(r.actor_mid, None, None)
                )
                if len(users) >= _MAX_USERS_PER_ITEM:
                    break

            # ---- Phase L2：评论层级关系 / 正文（读取时实时回捞，不冗余存储）----
            biz_id = latest.biz_id or ""
            idx = (
                comment_index.get(int(biz_id))
                if etype == EventTypeEnum.REPLY.value and biz_id.isdigit()
                else None
            )

            # 资源类型（1~6）：由 source_type 推导到 InteractionBizTypeEnum，
            # 统一承载动态 / 抽奖卡片 / RPA 操作等资源的互动通知（约束：resource_type 保持 int）。
            resource_type = _SOURCE_TYPE_TO_BIZ_TYPE.get(
                stype, InteractionBizTypeEnum.DYNAMIC
            ).value
            # 评论所属资源 ID：替代原 biz_id + subject_id，与 resource_type 共同唯一定位原资源。
            resource_id = ""
            # 评论层级关系（必填，读取时补全；一级评论时 root_id == source_id、target_id 空串）。
            root_id = ""
            source_id = biz_id  # 当前评论 / 消息自身唯一 ID
            target_id = ""
            source_content = ""
            target_content = ""
            if idx is not None:
                root_pk = idx.root or 0
                parent_pk = idx.parent or 0
                target_pk = parent_pk if parent_pk else root_pk
                resource_id = str(idx.oid)
                # 一级评论（root == 自身）时 root_id 等于 source_id
                root_id = str(root_pk) if root_pk else source_id
                # 直接回复目标；一级评论无目标则为空串
                target_id = str(target_pk) if target_pk else ""
                source_content = comment_content.get(int(biz_id), "")
                # target 对应评论正文（楼中楼 / 根评论均展示，用于通知上下文）
                if target_id and target_id.isdigit():
                    target_content = comment_content.get(int(target_id), "")
            else:
                # 非评论回复场景（点赞 / @ / 动态类）：resource_id 取 biz_id（资源 / 实体 id）
                resource_id = biz_id if biz_id.isdigit() else ""

            # 标题 / 封面 / 跳转链接：读取时按 source_type + source_id 实时回捞原资源，不冗余存储
            title, image, uri = await _resolve_source_meta(
                session, latest.source_type, latest.source_id, latest.biz_id, dyn_cache
            )

            return EventMsgfeedItem(
                id=latest.id or 0,
                users=users,
                item=EventMsgfeedContent(
                    item_id=latest.id or 0,
                    type=etype,
                    business=latest.source_type.value if latest.source_type else 0,
                    resource_type=resource_type,
                    resource_id=resource_id,
                    root_id=root_id,
                    source_id=source_id,
                    target_id=target_id,
                    title=title,
                    desc=latest.content,
                    image=image,
                    uri=uri,
                    source_content=source_content,
                    target_content=target_content,
                    ctime=int(latest.created_at.timestamp()) if latest.created_at else 0,
                ),
                counts=cnt,
                notice_state=0,
            )

        total_items: list[EventMsgfeedItem] = []
        for etype, stype, sid, cnt, _unread, latest_id in groups:
            rows = bucket.get((etype, stype, sid), [])
            total_items.append(
                await _to_item(
                    etype.value if hasattr(etype, "value") else str(etype),
                    stype,
                    rows,
                    int(cnt or 0),
                    session,
                    dyn_cache,
                )
            )

        # 再取一条判断是否还有下一页
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

        return EventListResp(
            latest=EventMsgfeedSection(items=total_items[:1]),
            total=EventMsgfeedSection(cursor=cursor, items=total_items),
        )

    # ==================== 已读管理 ====================

    @staticmethod
    async def mark_read(
        session: AsyncSession, mid: int, req: EventReadReq
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

        # 同步推进类型级已读游标，便于「全部已读」后快速判断红点
        if req.event_type is not None and not req.event_ids:
            await EventService._advance_cursor(session, mid, req.event_type)

        await session.commit()
        return EventReadResp(
            affected=affected,
            unread_count=await EventService.count_unread(session, mid, req.event_type),
        )

    @staticmethod
    async def delete(session: AsyncSession, mid: int, event_ids: list[int]) -> int:
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

    @staticmethod
    async def count_unread(
        session: AsyncSession, mid: int, event_type: EventTypeEnum | None = None
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

    @staticmethod
    async def count_unread_by_type(session: AsyncSession, mid: int) -> dict[str, int]:
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

    @staticmethod
    async def _advance_cursor(
        session: AsyncSession, mid: int, event_type: EventTypeEnum
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


__all__ = ["EventService", "build_dedup_key"]
