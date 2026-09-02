"""通用资源 Feed 引擎（2.36.0，承载 2.35.0 全量排序信号）。

任意资源（``bizType+bizId``）经 Provider 提供候选与计数后，统一由本引擎执行
推荐流水线（与资源类型无关）：

1. **EdgeRank 多维打分**（2.35.0）：``compute_moment_score(stat, pubTime, profile, extra)``
   ——计数用 ``TInteractionStat``，附加维度用 ``EdgeRankExtra``；
2. **个性化 boost（登录，2.33.0 + 2.35.0 反馈闭环）**：关注作者 / 点赞过作者 /
   ``last_clicklist`` 点击作者 / 偏好话题 / 点击话题；
3. **匿名随机权重（未登录，2.34.0）**：以 ``uniq_id`` 为种子派生扰动 Profile；
4. **去重（2.32.0）**：排除 ``last_showlist``（客户端已展示资源 id）；
5. **分页**：取前 ``page_size``，``has_more`` = 排除后仍有剩余。

数据来源约定（2.36.0 计数统一）：
- 候选：``TResourceFeed``（``auditStatus='normal'`` + ``pubTime`` 窗口，统一元数据）；
- 计数：``TInteractionStat``（like/view/favorite/share/dislike/repost）+ ``CommentSubject``
  （comment 实时计数优先）；
- 资源特征（rich / is_forward）与作者质量表由 Provider 按 bizType 填充；
- 渲染：由调用方（Provider）按 ``bizType`` 分发（dynamic → ``TMoment`` 内容模块；
  其他 → RPC 详情）。
"""

from datetime import datetime

from pydantic import ConfigDict
from sqlmodel import Field, SQLModel, col, select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.core.config import settings
from app.models.db import (
    CommentSubject,
    MomentAuthorQuality,
    TInteractionStat,
    TMoment,
    TResourceLike,
    TMomentTopicRel,
)
from app.services.moment.edgerank import (
    FEED_PROFILE,
    EdgeRankExtra,
    build_anon_profile,
    build_moment_counts,
    compute_moment_score,
)
from app.services.user.follow import FollowService


class ResourceReportCount(SQLModel):
    """按 biz_id 聚合的待处理举报数（EdgeRank 降权输入，2.44.0）。"""

    biz_id: int
    pending_count: int


class FeedCandidate(SQLModel):
    """通用 Feed 候选（排序所需最小元数据 + 资源特征）。"""

    biz_type: str
    biz_id: int
    mid: int | None = None  # 作者 UID（2.37.0 起可空：lottery/rpa_* 等资源可能无作者）
    pub_time: datetime | None = None
    tags: list[int] = Field(default_factory=list)
    is_forward: bool = False
    rich: bool = False


class PersonalSignals(SQLModel):
    """登录用户个性化信号（2.33.0 + 2.35.0）。"""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    follow: set[int] = Field(default_factory=set)
    liked_author: set[int] = Field(default_factory=set)
    topic: set[int] = Field(default_factory=set)
    clicked_author: set[int] = Field(default_factory=set)
    clicked_topic: set[int] = Field(default_factory=set)


async def load_personal_signals(
    session: AsyncSession,
    viewer_mid: int,
    clicklist: list[int] | None = None,
) -> PersonalSignals:
    """加载推荐流个性化信号（2.33.0 + 2.35.0 反馈闭环）。

    - ``follow``：关注作者（``msg_user_follow``）；
    - ``liked_author`` / ``topic``：最近 ``edgerank_personalized_like_history_limit``
      条点赞历史（``TResourceLike``）批量 join ``TMoment`` / ``TMomentTopicRel``；
    - ``clicked_author`` / ``clicked_topic``：``last_clicklist``（客户端已互动列表）
      对应作者/话题（2.35.0 反馈闭环）。

    任一信号为空不影响其余（打分时缺省按 0）。
    """
    signals = PersonalSignals()

    follow_mids = await FollowService.list_following_mids(session, viewer_mid)
    signals.follow = {int(m) for m in follow_mids}

    clicks = list(dict.fromkeys(clicklist or []))
    if clicks:
        ca_rows = (
            await session.exec(
                select(TMoment.mid).where(col(TMoment.dynId).in_(clicks))
            )
        ).all()
        signals.clicked_author = {int(r) for r in ca_rows}
        ct_rows = (
            await session.exec(
                select(TMomentTopicRel.topicId).where(
                    col(TMomentTopicRel.dynId).in_(clicks)
                )
            )
        ).all()
        signals.clicked_topic = {int(r) for r in ct_rows}

    limit = settings.edgerank_personalized_like_history_limit
    if limit <= 0:
        return signals

    like_rows = (
        await session.exec(
            select(TResourceLike.bizId)
            .where(
                col(TResourceLike.bizType) == InteractionBizTypeEnum.DYNAMIC,
                col(TResourceLike.mid) == viewer_mid,
                col(TResourceLike.bizId).isnot(None),
            )
            .order_by(col(TResourceLike.created_at).desc())
            .limit(limit)
        )
    ).all()
    liked_dyn_ids = [int(r) for r in like_rows if r]
    if not liked_dyn_ids:
        return signals

    author_rows = (
        await session.exec(
            select(TMoment.mid).where(col(TMoment.dynId).in_(liked_dyn_ids))
        )
    ).all()
    signals.liked_author = {int(r) for r in author_rows}
    topic_rows = (
        await session.exec(
            select(TMomentTopicRel.topicId).where(
                col(TMomentTopicRel.dynId).in_(liked_dyn_ids)
            )
        )
    ).all()
    signals.topic = {int(r) for r in topic_rows}
    return signals


def _boost(c: FeedCandidate, signals: PersonalSignals) -> float:
    """登录用户个性化加分（2.33.0 + 2.35.0）：关注/互动作者、偏好/点击话题。"""
    b = 0.0
    if c.mid in signals.follow:
        b += settings.edgerank_personalized_follow_weight
    if c.mid in signals.liked_author:
        b += settings.edgerank_personalized_liked_author_weight
    if c.mid in signals.clicked_author:
        b += settings.edgerank_click_weight
    pref_topics = signals.topic | signals.clicked_topic
    if pref_topics.intersection(c.tags):
        b += settings.edgerank_personalized_topic_weight
        if signals.clicked_topic.intersection(c.tags):
            b += settings.edgerank_click_weight
    return b


def _extra(
    c: FeedCandidate,
    stat: TInteractionStat,
    author_quality: dict[int, MomentAuthorQuality] | None,
    report_counts: dict[int, ResourceReportCount] | None = None,
    *,
    comment_subjects: dict[int, CommentSubject] | None = None,
    use_last_activity: bool = False,
) -> EdgeRankExtra:
    """附加维度信号（2.35.0 + 2.37.0 + 2.43.0 + 2.44.0；2.45.0 索引 dict 语义固化）。

    互动率 / 丰富度 / 转发 / 曝光 / 点踩 / 作者质量（含粉丝/等级）/
    举报数（``report_count``，pending 举报降权，全资源通用）。
    2.43.0：``use_last_activity`` 开启时注入「最近活跃时间」（最后评论时间），
    供 EdgeRank 时间衰减基准使用。

    ``author_quality`` / ``report_counts`` / ``comment_subjects`` 均为
    ``{id: SQLModel 实体}`` 批量查询结果索引（一次 IN 查询后点查，避免 N+1），
    dict 仅作映射容器、值一律为具体实体（2.45.0 起不再出现裸 tuple/dict 值）。
    """
    like = int(stat.likeCount or 0)
    comment = int(stat.commentCount or 0)
    repost = int(stat.repostCount or 0)
    view = int(stat.viewCount or 0)
    dislike = int(stat.dislikeCount or 0)
    extra = EdgeRankExtra(
        engagement=(like + comment + repost) / max(view, 1),
        rich=c.rich,
        is_forward=c.is_forward,
        exposure=float(view),
        dislike_ratio=dislike / max(dislike + like, 1),
    )
    if use_last_activity and comment_subjects:
        cs = comment_subjects.get(c.biz_id)
        last_at = cs.updated_at if cs is not None else None
        if last_at is not None:
            extra.last_activity_time = (
                max(c.pub_time, last_at) if c.pub_time is not None else last_at
            )
    aq = author_quality.get(c.mid) if author_quality and c.mid is not None else None
    if aq:
        extra.author_quality = aq.avgEngagement
        extra.recent_publish = aq.recentPublishCount
        extra.fans = aq.fansCount
        extra.level = float(aq.currentLevel)
    if report_counts:
        rc = report_counts.get(c.biz_id)
        if rc:
            extra.report_count = float(rc.pending_count)
    return extra


async def rank_feed(
    session: AsyncSession,
    candidates: list[FeedCandidate],
    counts: dict[int, TInteractionStat],
    *,
    viewer_mid: int | None,
    last_showlist: list[int] | None,
    uniq_id: str | None,
    page_size: int,
    clicklist: list[int] | None = None,
    author_quality: dict[int, MomentAuthorQuality] | None = None,
    report_counts: dict[int, ResourceReportCount] | None = None,
    comment_subjects: dict[int, CommentSubject] | None = None,
    now: datetime | None = None,
) -> tuple[list[int], bool]:
    """通用推荐流水线，返回 ``(排序后的 biz_id 列表, has_more)``。

    Args:
        candidates: Provider 提供的候选（已按 bizType 过滤、含 pubTime/tags/特征）；
        counts: ``{biz_id: TInteractionStat}``；
        clicklist: 客户端已互动 biz_id 列表（2.35.0 反馈闭环）；
        author_quality: ``{mid: MomentAuthorQuality}``（作者质量表，仅 dynamic 等有聚合的
          bizType 提供；读 ``avgEngagement`` / ``recentPublishCount`` /
          ``fansCount`` / ``currentLevel``）；
        report_counts: ``{biz_id: ResourceReportCount}``（pending 举报数，全资源通用降权）；
        comment_subjects: ``{biz_id: CommentSubject}``（``root_count`` 实时计数与
          ``updated_at`` 最后评论时间）；
        viewer_mid: 登录用户（个性化信号）；None → 匿名（随机权重 / 全局）；
        last_showlist: 客户端已展示 biz_id 列表（去重）；
        uniq_id: 客户端唯一 ID（匿名随机种子）；
        page_size: 单页条数。
    """
    now = now or datetime.now()

    # 2.34.0：未登录不用全局排序——uniq_id 派生随机权重；登录走 FEED_PROFILE
    if viewer_mid is None and settings.edgerank_anon_randomize_enabled:
        profile = build_anon_profile(uniq_id)
    else:
        profile = FEED_PROFILE

    # 2.33.0 + 2.35.0：个性化信号（登录 + 开关开启）
    personalized = viewer_mid is not None and settings.edgerank_personalized_enabled
    signals: PersonalSignals | None = None
    if personalized:
        signals = await load_personal_signals(session, viewer_mid, clicklist)

    def _score(c: FeedCandidate) -> float:
        cs = comment_subjects.get(c.biz_id) if comment_subjects else None
        cm = build_moment_counts(
            counts.get(c.biz_id), cs.root_count if cs is not None else None
        )
        s = compute_moment_score(
            cm,
            c.pub_time,
            profile,
            now=now,
            extra=_extra(
                c,
                cm,
                author_quality,
                report_counts,
                comment_subjects=comment_subjects,
                use_last_activity=settings.edgerank_decay_use_last_activity,
            ),
        )
        if personalized and signals is not None:
            s += _boost(c, signals)
        return s

    ranked = sorted(candidates, key=_score, reverse=True)

    # 2.32.0：推荐流无 page/offset 语义——排除已展示项后取前 page_size 条
    shown = set(last_showlist or ())
    if shown:
        ranked = [c for c in ranked if c.biz_id not in shown]
    page = ranked[:page_size]
    return [c.biz_id for c in page], len(ranked) > page_size


__all__ = [
    "FeedCandidate",
    "PersonalSignals",
    "ResourceReportCount",
    "load_personal_signals",
    "rank_feed",
]
