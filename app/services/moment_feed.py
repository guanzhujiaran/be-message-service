"""动态 Feed 流 & 详情服务（Phase 3）。

覆盖 P3-T1 ~ P3-T5：

- 综合页 Feed（P3-T1）：仅 ``auditStatus='normal'`` 且未软删，按 ``pubTime`` 倒序。
- 个人空间 Feed（P3-T2）：**本人视角**含 auditing/rejected（带状态标签），
  **访客视角**仅 normal；置顶动态优先。
- 动态详情（P3-T3）：非作者且非 normal → 返回 None（路由层转 404 / 占位卡）；
  本人可看全部状态。
- 批量详情（P3-T4）：批量 moment_id 查询（限 20 条），按请求者权限过滤。
- 分页游标（P3-T5）：``updateBaseline``（最新 dynId）/ ``historyOffset``（最旧 dynId）/
  ``hasMore`` / ``updateNum``。

渲染装配：author 模块经 ``PptrUserService.get_many`` 只读回查 pptr Postgres 取昵称/头像
（与评论系统一致，不冗余用户快照）；stat 模块直接从 ``TMomentStat`` 读计数数字字段
（严禁请求热路径做 COUNT 聚合）。
"""

from datetime import datetime
from typing import Any

from sqlalchemy import func
from sqlmodel import col, select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.db import TMoment, TMomentLike, TMomentStat, TMomentTopic, TMomentTopicRel
from app.models.db.comment import CommentSubject
from app.models.enums import (
    InteractionBizTypeEnum,
    MomentAuditStatusEnum,
    MomentTopicAuditStatusEnum,
    MomentTypeEnum,
)
from app.models.schemas.moment import (
    MomentContentNode,
    MomentDetailResp,
    MomentFeedItem,
    MomentFeedResp,
    MomentForwardItem,
    MomentForwardListResp,
    MomentLikerItem,
    MomentLikerListResp,
    MomentModule,
    MomentTopicFeedResp,
    MomentTopicRef,
)
from app.services.follow import FollowService
from app.services.pptr_user import PptrUserService

from loguru import logger

# RESOURCE=lottery 读取时实时回查详情（2.20.1，弱依赖降级）
_LOTTERY_BIZ_TYPE = "lottery"

# 单页上限
_FEED_PAGE_SIZE = 20
_DETAIL_BATCH_LIMIT = 20
# 转发嵌套最大深度（防转发链成环导致的无限递归）
_MAX_FORWARD_DEPTH = 3


def _iso(dt: datetime | None) -> str | None:
    """datetime → ISO 字符串（无 tz，与现有服务一致）。"""
    return dt.isoformat() if dt else None


def _build_stat_modules(
    stat: TMomentStat | None, comment_count_override: int | None = None
) -> dict[str, Any]:
    """从 TMomentStat 读计数快照（直接读字段，不做 COUNT 聚合）。

    ``comment_count_override``：评论系统实时计数（``msg_comment_subject.root_count``）。
    评论数若存在 subject 记录，一律以评论系统计数为准（TMomentStat.commentCount
    是跨模块回写，存量数据可能为 0 / 回写链路偶发遗漏）。
    """
    if stat is None:
        return {
            "likeCount": 0,
            "commentCount": comment_count_override if comment_count_override is not None else 0,
            "repostCount": 0,
            "viewCount": 0,
        }
    comment_count = stat.commentCount
    if comment_count_override is not None:
        comment_count = comment_count_override
    return {
        "likeCount": stat.likeCount,
        "commentCount": comment_count,
        "repostCount": stat.repostCount,
        "viewCount": stat.viewCount,
    }


def _collect_lottery_ids(dyns: list[TMoment]) -> list[int]:
    """收集一批动态 attach 卡（bizType=lottery）的 lottery_id（去重、保持顺序）。

    2.21.0 起 attach 卡存于 `TMoment.bizType`/`bizRid`（不再放正文 RESOURCE 节点）；
    兼容旧数据：仍回扫 contentJson 中 RESOURCE=lottery 节点。
    """
    ids: list[int] = []
    seen: set[int] = set()

    def _add(lid: object) -> None:
        try:
            lid_i = int(lid)
        except (TypeError, ValueError):
            return
        if lid_i not in seen:
            seen.add(lid_i)
            ids.append(lid_i)

    for dyn in dyns:
        # 新方案：attach 卡独立列（TMoment.bizType 为 IntEnum，与枚举成员比较）
        if dyn.bizType == InteractionBizTypeEnum.LOTTERY and dyn.bizRid:
            _add(dyn.bizRid)
            continue
        # 兼容旧数据：正文 RESOURCE 节点
        content = dyn.contentJson if isinstance(dyn.contentJson, list) else None
        if not content:
            continue
        for node in content:
            if not isinstance(node, dict):
                continue
            if node.get("type") == "RESOURCE" and node.get("bizType") == _LOTTERY_BIZ_TYPE:
                _add(node.get("bizId"))
    return ids


async def _build_lottery_detail_map(
    dyns: list[TMoment],
) -> dict[int, dict[str, str | None]]:
    """对一批动态收集 lottery_id 后批量一次 RPC 回查详情（2.20.1）。

    返回 ``{lottery_id: {name, cover, jumpUrl}}``；RPC 失败 / 未连接 / 无 lottery 时
    返回空 dict（弱依赖降级），调用方保留原节点。
    """
    ids = _collect_lottery_ids(dyns)
    if not ids:
        return {}
    from app.services.lottery_rpc import get_lottery_rpc_client

    client = await get_lottery_rpc_client()
    try:
        return await client.get_lottery_details(ids)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[moment_feed] 批量回查 lottery 详情失败，降级保留原节点: {e}")
        return {}


def _enrich_resource_nodes(
    nodes: list[MomentContentNode] | None,
    detail_map: dict[int, dict[str, str | None]],
) -> None:
    """用批量回查结果填充 RESOURCE=lottery 节点详情（2.20.1）。

    数据库 contentJson 只落 bizType/bizId/name（不存卡片快照），读取时经批量 RPC
    实时获取的 detail_map 填充 name/cover/jumpUrl；不在 map 中（RPC 失败/资源不存在）
    则保留原节点（name 已有，降级可用）。
    """
    if not nodes or not detail_map:
        return
    for n in nodes:
        if n.type == "RESOURCE" and n.bizType == _LOTTERY_BIZ_TYPE and n.bizId:
            try:
                detail = detail_map.get(int(n.bizId))
            except (TypeError, ValueError):
                detail = None
            if detail:
                n.name = detail["name"] or n.name
                n.cover = detail["cover"] or n.cover
                n.jumpUrl = detail["jumpUrl"] or n.jumpUrl


async def _build_feed_item(
    dyn: TMoment,
    stat: TMomentStat | None,
    *,
    session: AsyncSession | None = None,
    author: Any | None = None,
    is_like: bool | None = None,
    comment_count_override: int | None = None,
    lottery_detail_map: dict[int, dict[str, str | None]] | None = None,
    topic_rel_map: dict[int, list[int]] | None = None,
    _depth: int = 0,
) -> MomentFeedItem:
    """把 TMoment + TMomentStat 装配成对外卡片。

    author 由调用方批量取回后传入（``PptrUserService.get_many`` 结果），
    避免逐条回查造成 N+1。

    FORWARD 类型且传入 session 时，会递归加载源动态并嵌套到
    ``forward.srcMoment``（最多嵌套 ``_MAX_FORWARD_DEPTH`` 层，防环）。

    ``comment_count_override``：评论系统实时计数（``msg_comment_subject.root_count``），
    有值则覆盖 TMomentStat.commentCount（评论系统自身计数为准）。
    """
    modules: list[MomentModule] = []

    # author 模块
    modules.append(
        MomentModule(
            moduleType="author",
            mid=dyn.mid,
            uname=author.uname if author else None,
            face=author.avatar if author else None,
            ptimeLabelText=_iso(dyn.pubTime),
            relation=None,
        )
    )

    # desc 模块（富文本节点）
    nodes = None
    if isinstance(dyn.contentJson, list):
        nodes = [
            MomentContentNode.model_validate(n) if isinstance(n, dict) else n
            for n in dyn.contentJson
        ]
        # 2.20.1：旧数据正文 RESOURCE=lottery 节点用批量回查结果填充（不存快照，弱依赖降级）
        _enrich_resource_nodes(nodes, lottery_detail_map or {})
    modules.append(
        MomentModule(
            moduleType="desc",
            text=dyn.contentText,
            nodes=nodes,
        )
    )

    # additional（附加卡）模块（2.21.0）：渲染于 desc 正文下方，只存 bizType+bizId，
    # name/cover/jumpUrl 由装配层 RPC 实时获取（弱依赖失败仅返回 bizType+bizId）
    if dyn.bizType and dyn.bizRid:
        detail = (lottery_detail_map or {}).get(int(dyn.bizRid)) if dyn.bizType == InteractionBizTypeEnum.LOTTERY else None
        modules.append(
            MomentModule(
                moduleType="additional",
                bizType=dyn.bizType.to_text(),
                bizId=str(dyn.bizRid),
                name=detail["name"] if detail else None,
                cover=detail["cover"] if detail else None,
                jumpUrl=detail["jumpUrl"] if detail else None,
            )
        )

    # dynamic 正文卡模块
    modules.append(
        MomentModule(
            moduleType="dynamic",
            dtype="forward" if dyn.dynType is MomentTypeEnum.FORWARD else "word",
            text=None,
        )
    )

    # forward 嵌套模块（仅 FORWARD）：嵌套原动态完整卡片
    if dyn.dynType is MomentTypeEnum.FORWARD and dyn.repostSrcDynId:
        src_moment: MomentFeedItem | None = None
        if session is not None and _depth < _MAX_FORWARD_DEPTH:
            src_dyn = (
                await session.exec(
                    select(TMoment)
                    .where(col(TMoment.dynId) == dyn.repostSrcDynId)
                    .where(col(TMoment.deletedAt).is_(None))
                )
            ).one_or_none()
            if src_dyn is not None:
                src_stat = (
                    await session.exec(
                        select(TMomentStat).where(col(TMomentStat.dynId) == src_dyn.dynId)
                    )
                ).one_or_none()
                # 2.22.0：嵌套源动态的话题关系若未在外层 map，单条补齐（仅 FORWARD 嵌套场景，量小）
                nested_rel_map = topic_rel_map or {}
                if src_dyn.dynId not in nested_rel_map:
                    extra = await _load_topic_rel_map(session, [src_dyn.dynId])
                    if extra:
                        nested_rel_map = {**nested_rel_map, **extra}
                src_moment = await _build_feed_item(
                    src_dyn,
                    src_stat,
                    session=session,
                    topic_rel_map=nested_rel_map,
                    _depth=_depth + 1,
                )
        modules.append(
            MomentModule(
                moduleType="forward",
                srcDynId=dyn.repostSrcDynId,
                srcMoment=src_moment,
            )
        )

    # extend 模块（话题，2.22.0 支持多话题：关系表 + 主话题并集，主话题排首位）
    topic_ids: list[int] = []
    if dyn.topicId:
        topic_ids.append(dyn.topicId)
    for tid in (topic_rel_map or {}).get(dyn.dynId, []):
        if tid not in topic_ids:
            topic_ids.append(tid)
    if topic_ids:
        modules.append(
            MomentModule(
                moduleType="extend",
                topicId=topic_ids[0],
                topics=[MomentTopicRef(topicId=t) for t in topic_ids],
            )
        )

    # stat 模块
    stat_snapshot = _build_stat_modules(stat, comment_count_override=comment_count_override)
    modules.append(
        MomentModule(
            moduleType="stat",
            likeCount=stat_snapshot["likeCount"],
            commentCount=stat_snapshot["commentCount"],
            repostCount=stat_snapshot["repostCount"],
            viewCount=stat_snapshot["viewCount"],
        )
    )

    # interaction 模块（当前用户点赞态）
    modules.append(MomentModule(moduleType="interaction", isLike=is_like))

    return MomentFeedItem(
        dynId=dyn.dynId,
        dynIdStr=str(dyn.dynId),
        dynType=dyn.dynType.name,
        mid=dyn.mid,
        auditStatus=dyn.auditStatus.value,
        isTop=dyn.isTop,
        pubTime=_iso(dyn.pubTime),
        createdTime=_iso(dyn.created_at),
        auditRejectReason=dyn.auditRejectReason,
        # IP 属地：数据库无值（旧数据/未解析）时兜底为「未知」，不返回 None
        ipLocation=dyn.ipLocation or "未知",
        ipIsp=dyn.ipIsp,
        modules=modules,
        stat=stat_snapshot,
    )


async def _attach_authors(
    session: AsyncSession, items: list[MomentFeedItem]
) -> None:
    """批量回查作者信息并回填 author 模块（一次 IN 查询，无 N+1）。

    递归处理嵌套的 ``forward.srcMoment``（转发原动态的作者信息一并回填）。
    """
    # 先递归收集所有层级卡片（含嵌套原动态）
    all_items: list[MomentFeedItem] = []

    def _collect(cards: list[MomentFeedItem]) -> None:
        for it in cards:
            all_items.append(it)
            for m in it.modules:
                if m.moduleType == "forward" and m.srcMoment is not None:
                    _collect([m.srcMoment])

    _collect(items)
    mids = {it.mid for it in all_items}
    if not mids:
        return
    briefs = await PptrUserService.get_many(list(mids))
    for it in all_items:
        b = briefs.get(it.mid)
        for m in it.modules:
            if m.moduleType == "author":
                m.uname = b.uname if b else None
                m.face = b.avatar if b else None


async def _attach_topics(
    session: AsyncSession, items: list[MomentFeedItem]
) -> None:
    """批量回查话题名称并回填 extend 模块（一次 IN 查询，无 N+1）。

    2.22.0 起 extend 模块含 ``topics[]`` 多话题数组，一并回填 topicName；
    兼容保留 ``topicId``/``topicName`` 单话题字段（= topics[0]）。
    递归处理嵌套的 ``forward.srcMoment``（转发原动态的话题一并回填）。
    话题已删除 / 不存在时保留 ``topicName=None``，由前端兜底显示
    ``#话题 {topicId}#``。
    """
    # 先递归收集所有层级卡片（含嵌套原动态）
    all_items: list[MomentFeedItem] = []

    def _collect(cards: list[MomentFeedItem]) -> None:
        for it in cards:
            all_items.append(it)
            for m in it.modules:
                if m.moduleType == "forward" and m.srcMoment is not None:
                    _collect([m.srcMoment])

    _collect(items)
    topic_ids: set[int] = set()
    for it in all_items:
        for m in it.modules:
            if m.moduleType != "extend":
                continue
            if m.topicId is not None:
                topic_ids.add(m.topicId)
            for t in m.topics or []:
                if t.topicId is not None:
                    topic_ids.add(t.topicId)
    if not topic_ids:
        return
    rows = (
        await session.exec(
            select(TMomentTopic).where(col(TMomentTopic.topicId).in_(topic_ids))
        )
    ).all()
    name_map = {r.topicId: r.topicName for r in rows}
    for it in all_items:
        for m in it.modules:
            if m.moduleType != "extend":
                continue
            if m.topicId is not None:
                m.topicName = name_map.get(m.topicId)
            for t in m.topics or []:
                t.topicName = name_map.get(t.topicId)


async def _load_topic_rel_map(
    session: AsyncSession, moment_ids: list[int]
) -> dict[int, list[int]]:
    """批量读动态-话题多对多关系（2.22.0），按 dynId 分组，保持写入顺序。

    返回 ``{dynId: [topicId, ...]}``；无关系返回空 dict。
    """
    if not moment_ids:
        return {}
    rows = (
        await session.exec(
            select(TMomentTopicRel.dynId, TMomentTopicRel.topicId)
            .where(col(TMomentTopicRel.dynId).in_(moment_ids))
            .order_by(col(TMomentTopicRel.pk))
        )
    ).all()
    result: dict[int, list[int]] = {}
    for dyn_id, topic_id in rows:
        result.setdefault(int(dyn_id), []).append(int(topic_id))
    return result


async def _load_stats(
    session: AsyncSession, moment_ids: list[int]
) -> dict[int, TMomentStat]:
    """批量读 TMomentStat（直接读字段，禁 COUNT 聚合）。"""
    if not moment_ids:
        return {}
    rows = (
        await session.exec(select(TMomentStat).where(col(TMomentStat.dynId).in_(moment_ids)))
    ).all()
    return {r.dynId: r for r in rows}


async def _load_comment_counts(
    session: AsyncSession, moment_ids: list[int]
) -> dict[int, int]:
    """批量读评论系统实时计数（msg_comment_subject.root_count，type='dynamic'）。

    以评论系统自身维护的冗余计数为准（TMomentStat.commentCount 是跨模块回写，
    存量数据可能为 0 或回写链路偶发遗漏）。单次 IN 查询，非 COUNT 聚合。
    """
    if not moment_ids:
        return {}
    rows = (
        await session.exec(
            select(CommentSubject.oid, CommentSubject.root_count).where(
                col(CommentSubject.oid).in_(moment_ids),
                col(CommentSubject.type) == "dynamic",
            )
        )
    ).all()
    return {int(r[0]): int(r[1]) for r in rows}


async def _load_like_states(
    session: AsyncSession, moment_ids: list[int], viewer_mid: int | None
) -> dict[int, bool]:
    """批量读当前用户对这批动态的点赞态（用于 interaction 模块）。"""
    if not moment_ids or not viewer_mid:
        return {}
    rows = (
        await session.exec(
            select(TMomentLike.dynId)
            .where(col(TMomentLike.dynId).in_(moment_ids))
            .where(col(TMomentLike.mid) == viewer_mid)
        )
    ).all()
    return {int(d): True for d in rows}


class MomentFeedService:
    """动态 Feed / 详情服务（静态方法集合，无状态）。"""

    # ==================== 综合页 Feed（P3-T1）====================

    @staticmethod
    async def comprehensive_feed(
        session: AsyncSession,
        *,
        page: int = 1,
        page_size: int = _FEED_PAGE_SIZE,
        update_baseline: int | None = None,
        history_offset: int | None = None,
        refresh_type: int = 1,
        viewer_mid: int | None = None,
    ) -> MomentFeedResp:
        """综合页 Feed：仅 normal + 未软删，pubTime 倒序，游标分页。"""
        page = max(1, page)
        page_size = min(max(1, page_size), 50)

        stmt = (
            select(TMoment)
            .where(col(TMoment.auditStatus) == MomentAuditStatusEnum.NORMAL)
            .where(col(TMoment.deletedAt).is_(None))
            .where(col(TMoment.pubTime).isnot(None))
        )

        if history_offset is not None:
            # 上拉加载：取比 history_offset 更旧的
            stmt = stmt.where(col(TMoment.dynId) < history_offset)
        if update_baseline is not None and refresh_type == 2:
            # 翻页场景也可基于基线
            stmt = stmt.where(col(TMoment.dynId) < update_baseline)

        stmt = stmt.order_by(col(TMoment.pubTime).desc()).limit(page_size + 1)
        rows = (await session.exec(stmt)).all()

        has_more = len(rows) > page_size
        page_rows = rows[:page_size]

        moment_ids = [r.dynId for r in page_rows]
        stats = await _load_stats(session, moment_ids)
        comment_counts = await _load_comment_counts(session, moment_ids)
        like_states = await _load_like_states(session, moment_ids, viewer_mid)
        # 2.20.1：本页 lottery 详情批量一次 RPC 回查，按动态分发填充 RESOURCE=lottery 节点
        lottery_detail_map = await _build_lottery_detail_map(page_rows)
        # 2.22.0：本页动态-话题多对多关系批量一次加载（无 N+1）
        topic_rel_map = await _load_topic_rel_map(session, moment_ids)

        items = [
            await _build_feed_item(
                r,
                stats.get(r.dynId),
                session=session,
                is_like=like_states.get(r.dynId),
                comment_count_override=comment_counts.get(r.dynId),
                lottery_detail_map=lottery_detail_map,
                topic_rel_map=topic_rel_map,
            )
            for r in page_rows
        ]
        await _attach_authors(session, items)
        await _attach_topics(session, items)

        baseline = items[0].dynId if items else None
        history = items[-1].dynId if items else None
        update_num = 0
        if refresh_type == 1 and update_baseline is not None:
            # 刷新：统计基线上方（dynId > baseline）的 normal 条数
            cnt = (
                await session.exec(
                    select(TMoment.dynId)
                    .where(col(TMoment.auditStatus) == MomentAuditStatusEnum.NORMAL)
                    .where(col(TMoment.deletedAt).is_(None))
                    .where(col(TMoment.pubTime).isnot(None))
                    .where(col(TMoment.dynId) > update_baseline)
                )
            ).all()
            update_num = len(cnt)

        return MomentFeedResp(
            items=items,
            hasMore=has_more,
            updateBaseline=baseline,
            historyOffset=history,
            updateNum=update_num,
        )

    # ==================== 关注流 Feed（仅关注的人的动态）====================

    @staticmethod
    async def following_feed(
        session: AsyncSession,
        *,
        viewer_mid: int,
        page: int = 1,
        page_size: int = _FEED_PAGE_SIZE,
        history_offset: int | None = None,
    ) -> MomentFeedResp:
        """关注流 Feed：仅展示**我关注的人**发布的 normal 动态。

        关注 mid 集合取自 ``msg_user_follow``（be-message 主库，
        ``FollowService.list_following_mids`` 全量拉取，按关注时间倒序）；
        动态过滤与综合页一致（normal + 未软删 + pubTime 非空），按 pubTime 倒序。
        复用综合页装配管线（``_build_feed_item`` / ``_attach_authors`` /
        ``_load_stats`` / ``_load_like_states``）。
        """
        page = max(1, page)
        page_size = min(max(1, page_size), 50)

        following_mids = await FollowService.list_following_mids(session, viewer_mid)
        if not following_mids:
            return MomentFeedResp(
                items=[],
                hasMore=False,
                updateBaseline=None,
                historyOffset=None,
                updateNum=0,
            )

        stmt = (
            select(TMoment)
            .where(col(TMoment.mid).in_(following_mids))
            .where(col(TMoment.auditStatus) == MomentAuditStatusEnum.NORMAL)
            .where(col(TMoment.deletedAt).is_(None))
            .where(col(TMoment.pubTime).isnot(None))
        )
        if history_offset is not None:
            stmt = stmt.where(col(TMoment.dynId) < history_offset)
        stmt = stmt.order_by(col(TMoment.pubTime).desc()).limit(page_size + 1)
        rows = (await session.exec(stmt)).all()

        has_more = len(rows) > page_size
        page_rows = rows[:page_size]

        moment_ids = [r.dynId for r in page_rows]
        stats = await _load_stats(session, moment_ids)
        comment_counts = await _load_comment_counts(session, moment_ids)
        like_states = await _load_like_states(session, moment_ids, viewer_mid)
        # 2.20.1：本页 lottery 详情批量一次 RPC 回查，按动态分发填充 RESOURCE=lottery 节点
        lottery_detail_map = await _build_lottery_detail_map(page_rows)
        # 2.22.0：本页动态-话题多对多关系批量一次加载（无 N+1）
        topic_rel_map = await _load_topic_rel_map(session, moment_ids)

        items = [
            await _build_feed_item(
                r,
                stats.get(r.dynId),
                session=session,
                is_like=like_states.get(r.dynId),
                comment_count_override=comment_counts.get(r.dynId),
                lottery_detail_map=lottery_detail_map,
                topic_rel_map=topic_rel_map,
            )
            for r in page_rows
        ]
        await _attach_authors(session, items)
        await _attach_topics(session, items)

        baseline = items[0].dynId if items else None
        history = items[-1].dynId if items else None
        return MomentFeedResp(
            items=items,
            hasMore=has_more,
            updateBaseline=baseline,
            historyOffset=history,
            updateNum=0,
        )

    # ==================== 话题 Feed（P5-T2）====================

    @staticmethod
    async def topic_feed(
        session: AsyncSession,
        *,
        topic_id: int,
        page: int = 1,
        page_size: int = _FEED_PAGE_SIZE,
        viewer_mid: int | None = None,
        history_offset: int | None = None,
        sort: str = "hot",
    ) -> MomentTopicFeedResp:
        """话题下动态流：以 ``topicId`` 过滤，仅 normal + 未软删。

        - ``sort="time"``：按 ``pubTime`` 倒序（最新）；
        - ``sort="hot"``（默认）：先按互动数（like+comment+repost）倒序取本页候选，
          再在内存里按互动数排序取前 page_size。

        复用综合页 Feed 的装配管线（``_build_feed_item`` / ``_attach_authors`` /
        ``_load_stats`` / ``_load_like_states``）。
        """
        page = max(1, page)
        page_size = min(max(1, page_size), 50)

        topic = (
            await session.exec(
                select(TMomentTopic).where(col(TMomentTopic.topicId) == topic_id)
            )
        ).one_or_none()
        # 2.19.0：话题 Feed 仅对审核通过的话题开放；不存在/非 normal 返回空流
        if topic is None or topic.auditStatus is not MomentTopicAuditStatusEnum.NORMAL:
            return MomentTopicFeedResp(topicId=topic_id, topicName="", items=[])
        topic_name = topic.topicName

        # 2.22.0：话题 Feed 双条件查询——主话题列（存量/主话题）+ 关系表（多话题动态）
        rel_dyn_ids = select(TMomentTopicRel.dynId).where(
            col(TMomentTopicRel.topicId) == topic_id
        )
        stmt = (
            select(TMoment)
            .where(col(TMoment.auditStatus) == MomentAuditStatusEnum.NORMAL)
            .where(col(TMoment.deletedAt).is_(None))
            .where(col(TMoment.pubTime).isnot(None))
            .where(
                (col(TMoment.topicId) == topic_id)
                | (col(TMoment.dynId).in_(rel_dyn_ids))
            )
        )
        if history_offset is not None:
            stmt = stmt.where(col(TMoment.dynId) < history_offset)

        # hot 排序需先回捞 stats，取 3 倍候选再按互动数倒序，避免漏掉高互动旧动态
        if sort == "time":
            stmt = stmt.order_by(col(TMoment.pubTime).desc()).limit(page_size + 1)
            rows = (await session.exec(stmt)).all()
            has_more = len(rows) > page_size
            page_rows = rows[:page_size]
        else:
            stmt = stmt.order_by(col(TMoment.pubTime).desc()).limit(page_size * 3 + 1)
            rows = (await session.exec(stmt)).all()
            candidates = rows[: page_size * 3]
            stats = await _load_stats(session, [r.dynId for r in candidates])
            comments = await _load_comment_counts(session, [r.dynId for r in candidates])
            ranked = sorted(
                candidates,
                key=lambda r: (
                    (stats.get(r.dynId).likeCount if stats.get(r.dynId) else 0)
                    + (comments.get(r.dynId) or 0)
                    + (stats.get(r.dynId).repostCount if stats.get(r.dynId) else 0)
                ),
                reverse=True,
            )
            page_rows = ranked[:page_size]
            # 若本页高互动动态数不足 page_size，追加最新动态补齐
            if len(page_rows) < page_size and len(candidates) > page_size:
                seen = {r.dynId for r in page_rows}
                for r in candidates:
                    if len(page_rows) >= page_size:
                        break
                    if r.dynId not in seen:
                        page_rows.append(r)
            has_more = len(candidates) > len(page_rows) or len(rows) > page_size * 3

        moment_ids = [r.dynId for r in page_rows]
        stats = await _load_stats(session, moment_ids)
        comment_counts = await _load_comment_counts(session, moment_ids)
        like_states = await _load_like_states(session, moment_ids, viewer_mid)
        # 2.20.1：本页 lottery 详情批量一次 RPC 回查，按动态分发填充 RESOURCE=lottery 节点
        lottery_detail_map = await _build_lottery_detail_map(page_rows)
        # 2.22.0：本页动态-话题多对多关系批量一次加载（无 N+1）
        topic_rel_map = await _load_topic_rel_map(session, moment_ids)

        items = [
            await _build_feed_item(
                r,
                stats.get(r.dynId),
                session=session,
                is_like=like_states.get(r.dynId),
                comment_count_override=comment_counts.get(r.dynId),
                lottery_detail_map=lottery_detail_map,
                topic_rel_map=topic_rel_map,
            )
            for r in page_rows
        ]
        await _attach_authors(session, items)
        await _attach_topics(session, items)

        baseline = items[0].dynId if items else None
        history = items[-1].dynId if items else None
        return MomentTopicFeedResp(
            topicId=topic_id,
            topicName=topic_name,
            items=items,
            hasMore=has_more,
            updateBaseline=baseline,
            historyOffset=history,
        )

    # ==================== 个人空间 Feed（P3-T2）====================

    @staticmethod
    async def space_feed(
        session: AsyncSession,
        host_mid: int,
        *,
        viewer_mid: int | None = None,
        page: int = 1,
        page_size: int = _FEED_PAGE_SIZE,
        history_offset: int | None = None,
    ) -> MomentFeedResp:
        """个人空间 Feed。

        本人视角：全部状态（auditing/rejected 带状态标签）；
        访客视角：仅 normal。置顶动态（仅 normal 可置顶）优先展示。
        """
        page = max(1, page)
        page_size = min(max(1, page_size), 50)
        is_self = viewer_mid is not None and viewer_mid == host_mid

        stmt = select(TMoment).where(col(TMoment.mid) == host_mid).where(
            col(TMoment.deletedAt).is_(None)
        )
        if not is_self:
            # 访客：仅 normal
            stmt = stmt.where(col(TMoment.auditStatus) == MomentAuditStatusEnum.NORMAL)
            stmt = stmt.where(col(TMoment.pubTime).isnot(None))

        if history_offset is not None:
            stmt = stmt.where(col(TMoment.dynId) < history_offset)

        # 置顶优先（仅 normal 可置顶），其次按时间倒序
        # 用 createdAt 排序（必有值，避免 auditing 状态 pubTime=NULL 排序异常）
        stmt = stmt.order_by(
            col(TMoment.isTop).desc(),
            col(TMoment.created_at).desc(),
        ).limit(page_size + 1)
        rows = (await session.exec(stmt)).all()

        has_more = len(rows) > page_size
        page_rows = rows[:page_size]

        moment_ids = [r.dynId for r in page_rows]
        stats = await _load_stats(session, moment_ids)
        comment_counts = await _load_comment_counts(session, moment_ids)
        like_states = await _load_like_states(session, moment_ids, viewer_mid)
        # 2.20.1：本页 lottery 详情批量一次 RPC 回查，按动态分发填充 RESOURCE=lottery 节点
        lottery_detail_map = await _build_lottery_detail_map(page_rows)
        # 2.22.0：本页动态-话题多对多关系批量一次加载（无 N+1）
        topic_rel_map = await _load_topic_rel_map(session, moment_ids)

        items = [
            await _build_feed_item(
                r,
                stats.get(r.dynId),
                session=session,
                is_like=like_states.get(r.dynId),
                comment_count_override=comment_counts.get(r.dynId),
                lottery_detail_map=lottery_detail_map,
                topic_rel_map=topic_rel_map,
            )
            for r in page_rows
        ]
        await _attach_authors(session, items)
        await _attach_topics(session, items)

        baseline = items[0].dynId if items else None
        history = items[-1].dynId if items else None
        return MomentFeedResp(
            items=items,
            hasMore=has_more,
            updateBaseline=baseline,
            historyOffset=history,
        )

    # ==================== 单条详情（P3-T3）====================

    @staticmethod
    async def get_detail(
        session: AsyncSession,
        moment_id: int,
        *,
        viewer_mid: int | None = None,
    ) -> MomentDetailResp | None:
        """动态详情。

        非作者且非 normal → 不可见，返回 None（路由层转 404 / 审核中占位）。
        作者本人可看全部状态（含 auditing/rejected）。
        """
        dyn = (
            await session.exec(select(TMoment).where(col(TMoment.dynId) == moment_id))
        ).one_or_none()
        if dyn is None or dyn.deletedAt is not None:
            return None
        if dyn.auditStatus != MomentAuditStatusEnum.NORMAL and dyn.mid != viewer_mid:
            return None

        stat = (
            await session.exec(select(TMomentStat).where(col(TMomentStat.dynId) == moment_id))
        ).one_or_none()
        comment_counts = await _load_comment_counts(session, [moment_id])
        like_states = await _load_like_states(session, [moment_id], viewer_mid)
        # 2.20.1：单条详情 lottery 详情批量回查（弱依赖降级）
        lottery_detail_map = await _build_lottery_detail_map([dyn])
        # 2.22.0：单条详情话题关系加载（无 N+1）
        topic_rel_map = await _load_topic_rel_map(session, [moment_id])
        item = await _build_feed_item(
            dyn,
            stat,
            session=session,
            is_like=like_states.get(moment_id),
            comment_count_override=comment_counts.get(moment_id),
            lottery_detail_map=lottery_detail_map,
            topic_rel_map=topic_rel_map,
        )
        await _attach_authors(session, [item])
        await _attach_topics(session, [item])
        return MomentDetailResp(**item.model_dump())

    # ==================== 批量详情（P3-T4）====================

    @staticmethod
    async def get_details_batch(
        session: AsyncSession,
        moment_ids: list[int],
        *,
        viewer_mid: int | None = None,
    ) -> list[MomentDetailResp]:
        """批量动态详情（限 20 条），按权限过滤。

        - 已软删 / 不存在 → 跳过；
        - 非 normal 且非作者 → 跳过。
        """
        if not moment_ids:
            return []
        moment_ids = list(dict.fromkeys(moment_ids))[:_DETAIL_BATCH_LIMIT]

        rows = (
            await session.exec(select(TMoment).where(col(TMoment.dynId).in_(moment_ids)))
        ).all()
        by_id = {r.dynId: r for r in rows}

        visible: list[TMoment] = []
        for did in moment_ids:
            dyn = by_id.get(did)
            if dyn is None or dyn.deletedAt is not None:
                continue
            if dyn.auditStatus != MomentAuditStatusEnum.NORMAL and dyn.mid != viewer_mid:
                continue
            visible.append(dyn)

        if not visible:
            return []

        stat_ids = [r.dynId for r in visible]
        stats = await _load_stats(session, stat_ids)
        comment_counts = await _load_comment_counts(session, stat_ids)
        like_states = await _load_like_states(session, stat_ids, viewer_mid)
        # 2.20.1：批量详情 lottery 详情一次 RPC 回查（弱依赖降级）
        lottery_detail_map = await _build_lottery_detail_map(visible)
        # 2.22.0：批量详情话题关系一次加载（无 N+1）
        topic_rel_map = await _load_topic_rel_map(session, stat_ids)

        items = [
            await _build_feed_item(
                r,
                stats.get(r.dynId),
                session=session,
                is_like=like_states.get(r.dynId),
                comment_count_override=comment_counts.get(r.dynId),
                lottery_detail_map=lottery_detail_map,
                topic_rel_map=topic_rel_map,
            )
            for r in visible
        ]
        await _attach_authors(session, items)
        await _attach_topics(session, items)
        return [MomentDetailResp(**it.model_dump()) for it in items]

    # ==================== 空间统计（对标 B 站 upstat）====================

    @staticmethod
    async def get_upstat(
        session: AsyncSession,
        mid: int,
    ) -> dict[str, int]:
        """统计某用户的空间数据（对标 B 站 `/x/space/upstat`）。

        返回对外可见（``auditStatus='normal'`` 且未软删）动态的统计：

        - ``dynamic_count``：动态总数；
        - ``like_count``：这些动态被点赞的总数（SUM(``TMomentStat.likeCount``)）。

        该接口为低频统计接口，允许做 COUNT / SUM 聚合。
        """
        visible = (
            col(TMoment.mid) == mid,
            col(TMoment.deletedAt).is_(None),
            col(TMoment.auditStatus) == MomentAuditStatusEnum.NORMAL,
        )

        dynamic_count = (
            await session.exec(
                select(func.count(TMoment.dynId)).where(*visible)
            )
        ).one()

        like_count = (
            await session.exec(
                select(func.coalesce(func.sum(TMomentStat.likeCount), 0)).where(
                    col(TMomentStat.dynId) == TMoment.dynId,
                    *visible,
                )
            )
        ).one()

        return {
            "dynamic_count": int(dynamic_count or 0),
            "like_count": int(like_count or 0),
        }

    # ==================== 点赞明细 / 转发列表（P8-T9）====================

    @staticmethod
    async def list_likers(
        session: AsyncSession,
        dyn_id: int,
        *,
        page_num: int = 1,
        page_size: int = 20,
    ) -> MomentLikerListResp:
        """点赞明细列表（按 TMomentLike.created_at 倒序）。

        数据源：``TMomentLike``（mid/dynId/created_at）。无 status 字段，
        任意点赞都算（与 ``MomentInteractionService.thumb`` 写入保持一致）。
        关联 ``PptrUserService.get_many`` 取作者简要（uname/face）。
        """
        page_num = max(1, page_num)
        page_size = min(max(1, page_size), 50)

        # 1) 总数（独立查询：使用 subquery 避免 N+1）
        total: int = 0
        like_count = (
            await session.exec(
                select(TMomentStat.likeCount).where(col(TMomentStat.dynId) == dyn_id)
            )
        ).one_or_none()
        if like_count is not None:
            total = int(like_count)

        # 2) 明细
        rows = (
            await session.exec(
                select(TMomentLike)
                .where(col(TMomentLike.dynId) == dyn_id)
                .order_by(col(TMomentLike.created_at).desc())
                .offset((page_num - 1) * page_size)
                .limit(page_size)
            )
        ).all()

        # 3) 关联用户简要（批量）
        briefs = await PptrUserService.get_many([r.mid for r in rows])
        items = [
            MomentLikerItem(
                mid=int(r.mid),
                uname=(b.uname if (b := briefs.get(r.mid)) else None),
                face=(b.avatar if (b := briefs.get(r.mid)) else None),
                like_time=_iso(r.created_at),
            )
            for r in rows
        ]
        return MomentLikerListResp(
            items=items,
            total=total,
            page_num=page_num,
            page_size=page_size,
        )

    @staticmethod
    async def list_forwards(
        session: AsyncSession,
        dyn_id: int,
        *,
        page_num: int = 1,
        page_size: int = 20,
    ) -> MomentForwardListResp:
        """转发该动态的列表（normal + 未软删，按 pubTime 倒序）。

        文本取 ``desc`` 模块文本（去除富文本节点）。如果 desc 是
        ``contentJson`` 里的模块，需要单独解析（contentText 是拼接版，含
        @等标记，这里取相对干净的第一段文本）。
        """
        page_num = max(1, page_num)
        page_size = min(max(1, page_size), 50)

        # 1) 基础查询
        base = (
            select(TMoment)
            .where(col(TMoment.repostSrcDynId) == dyn_id)
            .where(col(TMoment.auditStatus) == MomentAuditStatusEnum.NORMAL)
            .where(col(TMoment.deletedAt).is_(None))
        )

        # 2) 总数
        total: int = (
            await session.exec(
                select(func.count()).select_from(base.subquery())
            )
        ).one()

        # 3) 明细
        rows = (
            await session.exec(
                base.order_by(col(TMoment.pubTime).desc())
                .offset((page_num - 1) * page_size)
                .limit(page_size)
            )
        ).all()

        # 4) 关联用户简要
        briefs = await PptrUserService.get_many([r.mid for r in rows])
        items: list[MomentForwardItem] = []
        for r in rows:
            b = briefs.get(r.mid)
            text = _extract_first_text(r.contentJson)
            items.append(
                MomentForwardItem(
                    dynId=int(r.dynId),
                    mid=int(r.mid),
                    uname=(b.uname if b else None),
                    face=(b.avatar if b else None),
                    pubTime=_iso(r.pubTime),
                    text=text,
                )
            )
        return MomentForwardListResp(
            items=items,
            total=int(total or 0),
            page_num=page_num,
            page_size=page_size,
        )


def _extract_first_text(content_json: list | None) -> str | None:
    """从 contentJson 中提取第一条 WORDS 节点的纯文本（用于转发列表预览）。"""
    if not content_json:
        return None
    for node in content_json:
        if isinstance(node, dict) and node.get("type") == "WORDS" and node.get("text"):
            return str(node["text"])
    return None


__all__ = ["_DETAIL_BATCH_LIMIT", "_FEED_PAGE_SIZE", "MomentFeedService"]
