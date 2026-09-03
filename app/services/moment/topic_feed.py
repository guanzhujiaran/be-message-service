"""话题 Feed 流（话题下动态流）服务（参考动态综合页 Feed 重新实现，P5-T2）。

与动态综合页 ``MomentFeedService.comprehensive_feed`` 对齐（承接 2.46.0 召回 + 精排两段式）：

- **候选统一从通用 Feed 元数据表 ``TResourceFeed`` 召回**（bizType=dynamic，normal +
  PUBLIC + 未软删 + pubTime 非空），**话题范围**由「主话题列 ``TMoment.topicId`` ∪
  关系表 ``TMomentTopicRel``」得到该话题动态 bizId 集合后 ``bizId IN (...)`` 过滤
  （不依赖 ``TResourceFeed.tags`` JSON 包含查询，与 2.22.0 双条件查询一致）；
- **精排统一走 ``app.services.moment.feed_engine.rank_feed``**（EdgeRank 通用引擎），
  话题专用 Profile = ``TOPIC_FEED_PROFILE``（评论/转发权重更高、半衰期 6h，突出话题热点时效）；
- **``sort=recommend``（默认，与综合页一致）**：无 page/offset 游标，以 ``last_showlist``
  （已展示 dynId）去重，支持未登录匿名随机排序（基于 ``TOPIC_FEED_PROFILE`` 扰动）、
  登录用户个性化（关注/互动作者 boost）；``hasMore`` = 排除已展示后候选仍有剩余；
- **``sort=time``**：pubTime 倒序 + dynId 游标（``historyOffset``），与历史语义一致；
- **``sort=hot``**：``recommend`` 的兼容别名（历史客户端 ``sort=hot`` 仍生效）；
- 装配复用 ``moment_feed`` 的 ``_build_feed_item`` / ``_attach_authors`` / ``_attach_topics`` /
  ``_load_stats`` / ``_load_like_states`` / ``_load_comment_subjects`` /
  ``_build_lottery_detail_map`` / ``_load_topic_rel_map`` / ``_assemble_page``，无 N+1。

**不新增 ``bizType=topic`` 资源类型**：话题 Feed 展示的是「话题下的动态」（动态已是
``TResourceFeed`` 资源且携带话题 id），只需在动态候选召回阶段按话题过滤即可复用同一引擎；
``rank_feed`` 本就资源无关（``FeedCandidate``），话题 Profile 即「统一引擎」的实现。
"""

from datetime import datetime

from sqlmodel import col, func, select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.core.config import settings
from app.models.db import (
    MomentAuthorQuality,
    TMoment,
    TMomentTopic,
    TMomentTopicRel,
    TResourceFeed,
    TResourceReport,
)
from bili_common.models import InteractionBizTypeEnum
from bili_common.models.report import ReportAuditStatusEnum
from app.models.enums import (
    MomentAuditStatusEnum,
    MomentTopicAuditStatusEnum,
    MomentTypeEnum,
    MomentVisibleScopeEnum,
)
from app.models.schemas.moment import MomentTopicFeedResp
from app.services.moment.edgerank import TOPIC_FEED_PROFILE
from app.services.moment.feed_engine import (
    AuthorQualitySignal,
    FeedCandidate,
    ResourceReportCount,
    build_viewer_key,
    rank_feed,
)
# 装配管线复用 moment_feed（与综合页 / 空间 / 详情共用，避免重复实现与 N+1）
# 注意：moment_feed 不反向依赖本模块，无环；直接顶部导入即可。
from app.services.moment.moment_feed import (
    _assemble_page,
    _FEED_PAGE_SIZE,
    _is_rich_content,
    _load_comment_subjects,
    _load_stats,
)


async def _recall_topic_dyn_ids(session: AsyncSession, topic_id: int) -> list[int]:
    """收集某话题下的全部动态 bizId（主话题列 ∪ 关系表，去重保序）。

    与 2.22.0 话题 Feed 双条件查询一致：``TMoment.topicId = ?``（主话题）+ 关系表
    ``TMomentTopicRel.topicId = ?``。未在此处过滤 auditStatus/visibleScope——真正的
    Feed 资格由后续 ``TResourceFeed`` 召回（normal + PUBLIC + pubTime 非空）保证。
    """
    main_rows = (
        await session.exec(
            select(TMoment.dynId).where(col(TMoment.topicId) == topic_id)
        )
    ).all()
    rel_rows = (
        await session.exec(
            select(TMomentTopicRel.dynId).where(
                col(TMomentTopicRel.topicId) == topic_id
            )
        )
    ).all()
    ids: list[int] = []
    seen: set[int] = set()
    for r in list(main_rows) + list(rel_rows):
        rid = int(r)
        if rid not in seen:
            seen.add(rid)
            ids.append(rid)
    return ids


async def _recall_topic_feed_rows(
    session: AsyncSession,
    topic_dyn_ids: list[int],
    *,
    limit: int,
) -> list[TResourceFeed]:
    """按话题动态 bizId 集合召回 ``TResourceFeed`` 行（normal + PUBLIC + 未软删 + pubTime 非空）。

    与综合页 ``_recall_dynamic_candidates`` 末段回查 ``TResourceFeed`` 一致：直接过滤
    ``auditStatus`` / ``visibleScope`` / ``pubTime`` / ``deletedAt``，零 join。
    按 pubTime 倒序后截断 ``limit``，避免超热话题候选爆炸。
    """
    if not topic_dyn_ids:
        return []
    rows = (
        await session.exec(
            select(TResourceFeed)
            .where(
                col(TResourceFeed.bizType) == InteractionBizTypeEnum.DYNAMIC,
                col(TResourceFeed.bizId).in_(topic_dyn_ids),
                col(TResourceFeed.auditStatus) == "normal",
                col(TResourceFeed.deletedAt).is_(None),
                col(TResourceFeed.pubTime).isnot(None),
                col(TResourceFeed.visibleScope) == MomentVisibleScopeEnum.PUBLIC,
            )
            .order_by(col(TResourceFeed.pubTime).desc())
            .limit(limit)
        )
    ).all()
    return list(rows)


class TopicFeedService:
    """话题 Feed 服务（静态方法集合，无状态）。"""

    # ==================== 话题 Feed（P5-T2，参考综合页重新实现）====================

    @staticmethod
    async def topic_feed(
        session: AsyncSession,
        *,
        topic_id: int,
        page: int = 1,
        page_size: int = _FEED_PAGE_SIZE,
        viewer_mid: int | None = None,
        history_offset: int | None = None,
        sort: str = "recommend",
        last_showlist: list[int] | None = None,
        last_clicklist: list[int] | None = None,
        uniq_id: str | None = None,
    ) -> MomentTopicFeedResp:
        """话题下动态流。

        - ``sort="recommend"``（默认，2.46.0 起与综合页对齐）：候选从 ``TResourceFeed``
          召回该话题 normal+PUBLIC 动态，经 ``rank_feed``（话题专用 ``TOPIC_FEED_PROFILE``）
          EdgeRank 精排；无 page/offset 语义，以 ``last_showlist`` 去重，``hasMore`` =
          排除已展示后候选仍有剩余；``updateBaseline``/``historyOffset``/``updateNum`` 置空。
        - ``sort="time"``：pubTime 倒序 + dynId 游标（``historyOffset``），与历史语义一致。
        - ``sort="hot"``：``recommend`` 的兼容别名（历史客户端 ``sort=hot`` 仍生效）。
        """
        page = max(1, page)
        page_size = min(max(1, page_size), 50)

        topic = (
            await session.exec(
                select(TMomentTopic).where(col(TMomentTopic.topicId) == topic_id)
            )
        ).one_or_none()
        # 话题 Feed 仅对审核通过的话题开放；不存在/非 normal 返回空流
        if topic is None or topic.auditStatus is not MomentTopicAuditStatusEnum.NORMAL:
            return MomentTopicFeedResp(topicId=topic_id, topicName="", items=[])
        topic_name = topic.topicName

        # 话题范围动态 bizId（主话题列 ∪ 关系表）
        topic_dyn_ids = await _recall_topic_dyn_ids(session, topic_id)

        if sort == "time":
            # ---- 时间倒序 + dynId 游标（与 2.22.0 历史语义一致，不引入推荐流）----
            if not topic_dyn_ids:
                return MomentTopicFeedResp(
                    topicId=topic_id, topicName=topic_name, items=[]
                )
            rel_dyn_ids = select(TMomentTopicRel.dynId).where(
                col(TMomentTopicRel.topicId) == topic_id
            )
            stmt = (
                select(TMoment)
                .where(col(TMoment.auditStatus) == MomentAuditStatusEnum.NORMAL)
                .where(col(TMoment.deletedAt).is_(None))
                .where(col(TMoment.pubTime).isnot(None))
                .where(col(TMoment.visibleScope) == MomentVisibleScopeEnum.PUBLIC)
                .where(
                    (col(TMoment.topicId) == topic_id)
                    | (col(TMoment.dynId).in_(rel_dyn_ids))
                )
            )
            if history_offset is not None:
                stmt = stmt.where(col(TMoment.dynId) < history_offset)
            stmt = stmt.order_by(col(TMoment.pubTime).desc()).limit(page_size + 1)
            rows = (await session.exec(stmt)).all()
            has_more = len(rows) > page_size
            page_rows = rows[:page_size]
            items, baseline, history = await _assemble_page(
                session, page_rows, viewer_mid=viewer_mid
            )
            return MomentTopicFeedResp(
                topicId=topic_id,
                topicName=topic_name,
                items=items,
                hasMore=has_more,
                updateBaseline=baseline,
                historyOffset=history,
            )

        # ---- recommend（默认）/ hot 别名：TResourceFeed 召回 + rank_feed 精排 ----
        feed_rows = await _recall_topic_feed_rows(
            session, topic_dyn_ids, limit=settings.edgerank_candidate_limit
        )
        if not feed_rows:
            return MomentTopicFeedResp(
                topicId=topic_id, topicName=topic_name, items=[]
            )

        cand_ids = [r.bizId for r in feed_rows]
        # 渲染所需动态主表（内容）批量一次拉取
        moment_rows = (
            await session.exec(
                select(TMoment).where(col(TMoment.dynId).in_(cand_ids))
            )
        ).all()
        moment_map = {r.dynId: r for r in moment_rows}

        # 计数 / 评论系统实体 / 作者质量 / pending 举报（与综合页 recommend 一致）
        cand_stats = await _load_stats(session, cand_ids)
        cand_comment_subjects = await _load_comment_subjects(session, cand_ids)

        cand_mids = {r.mid for r in feed_rows}
        # 2.47.0：映射为引擎通用信号 AuthorQualitySignal（引擎不再依赖动态专属表）
        author_q: dict[int, AuthorQualitySignal] = {}
        if cand_mids:
            aq_rows = (
                await session.exec(
                    select(MomentAuthorQuality).where(
                        col(MomentAuthorQuality.mid).in_(cand_mids)
                    )
                )
            ).all()
            author_q = {
                int(r.mid): AuthorQualitySignal(
                    avg_engagement=r.avgEngagement,
                    recent_publish=r.recentPublishCount,
                    fans=r.fansCount,
                    level=float(r.currentLevel),
                )
                for r in aq_rows
            }

        report_counts: dict[int, ResourceReportCount] = {}
        if cand_ids:
            rp_rows = (
                await session.exec(
                    select(TResourceReport.bizId, func.count())
                    .where(
                        col(TResourceReport.bizType)
                        == int(InteractionBizTypeEnum.DYNAMIC),
                        col(TResourceReport.bizId).in_(cand_ids),
                        col(TResourceReport.auditStatus)
                        == int(ReportAuditStatusEnum.PENDING),
                    )
                    .group_by(col(TResourceReport.bizId))
                )
            ).all()
            report_counts = {
                int(b): ResourceReportCount(biz_id=int(b), pending_count=int(c))
                for b, c in rp_rows
            }

        candidates: list[FeedCandidate] = []
        for r in feed_rows:
            dyn = moment_map.get(r.bizId)
            candidates.append(
                FeedCandidate(
                    biz_type="dynamic",
                    biz_id=r.bizId,
                    mid=r.mid,
                    pub_time=r.pubTime,
                    tags=list(r.tags or []),
                    is_forward=(
                        dyn.dynType is MomentTypeEnum.FORWARD if dyn else False
                    ),
                    rich=_is_rich_content(dyn.contentJson) if dyn else False,
                )
            )

        ranked_ids, has_more = await rank_feed(
            session,
            candidates,
            cand_stats,
            viewer_mid=viewer_mid,
            last_showlist=last_showlist,
            uniq_id=uniq_id,
            page_size=page_size,
            clicklist=last_clicklist,
            author_quality=author_q,
            report_counts=report_counts,
            comment_subjects=cand_comment_subjects,
            now=datetime.now(),
            profile=TOPIC_FEED_PROFILE,
            # 2.47.0 曝光去重：同一观众在话题流场景已下发过的动态不再重复下发
            viewer_key=build_viewer_key(viewer_mid, uniq_id),
            feed_scene="topic",
            biz_type=InteractionBizTypeEnum.DYNAMIC,
        )

        # 推荐流无 page/offset 语义；updateBaseline/historyOffset/updateNum 置空
        page_rows = [moment_map[bid] for bid in ranked_ids if bid in moment_map]
        items, _, _ = await _assemble_page(session, page_rows, viewer_mid=viewer_mid)
        return MomentTopicFeedResp(
            topicId=topic_id,
            topicName=topic_name,
            items=items,
            hasMore=has_more,
            updateBaseline=None,
            historyOffset=None,
            updateNum=0,
        )


__all__ = ["TopicFeedService", "_recall_topic_dyn_ids", "_recall_topic_feed_rows"]
