"""通用资源 Feed 引擎（2.36.0，承载 2.35.0 全量排序信号）。

任意资源（``bizType+bizId``）经 Provider 提供候选与计数后，统一由本引擎执行
推荐流水线（与资源类型无关）：

1. **EdgeRank 多维打分**（2.35.0；2.47.0 全量互动特征）：``compute_moment_score(stat, pubTime, profile, extra)``
   ——计数用 ``TInteractionStat``，附加维度用 ``EdgeRankExtra``（2.47.0 起 like/comment/
   repost/**favorite/share/coin**/view/dislike 全量参与特征构造，纯 stat 驱动、无资源类型分支）；
2. **个性化 boost（登录，2.33.0 + 2.35.0 反馈闭环）**：关注作者 / 点赞过作者 /
   ``last_clicklist`` 点击作者 / 偏好话题 / 点击话题；
3. **匿名随机权重（未登录，2.34.0）**：以 ``uniq_id`` 为种子派生扰动 Profile；
4. **去重（2.32.0 + 2.47.0 曝光去重）**：排除客户端 ``last_showlist`` ∪ 服务端
   ``TFeedImpression``（该观众在本场景 TTL 内已下发过的资源）；**尽力去重 + 自动降级**
   ——候选不足一页时逐级放宽，保证 feed 不因去重见底；
5. **分页**：取前 ``page_size``，``has_more`` = 排除后仍有剩余；下发后批量 upsert 曝光记录。

数据来源约定（2.36.0 计数统一）：
- 候选：``TResourceFeed``（``auditStatus='normal'`` + ``pubTime`` 窗口，统一元数据）；
- 计数：``TInteractionStat``（like/view/favorite/share/dislike/repost/coin）+ ``CommentSubject``
  （comment 实时计数优先）；
- 资源特征（rich / is_forward）与作者质量由 Provider 按 bizType 以**通用结构**注入
  （2.47.0：引擎不再依赖动态专属表 ``moment_author_quality``，改收 ``AuthorQualitySignal``）；
- 曝光去重：``TFeedImpression``（``viewerKey`` = ``mid:{mid}`` / ``anon:{uniq_id}``）；
- 渲染：由调用方（Provider）按 ``bizType`` 分发（dynamic → ``TMoment`` 内容模块；
  其他 → RPC 详情）。
"""
from bili_common.models import InteractionBizTypeEnum

from datetime import datetime, timedelta

from pydantic import ConfigDict
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlmodel import Field, SQLModel, col, select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.core.config import settings
from app.models.db import (
    CommentSubject,
    TFeedImpression,
    TInteractionStat,
    TMoment,
    TResourceLike,
    TMomentTopicRel,
)
from app.services.moment.edgerank import (
    FEED_PROFILE,
    EdgeRankExtra,
    EdgeRankProfile,
    build_anon_profile,
    build_moment_counts,
    compute_moment_score,
)
from app.services.user.follow import FollowService


class ResourceReportCount(SQLModel):
    """按 biz_id 聚合的待处理举报数（EdgeRank 降权输入，2.44.0）。"""

    biz_id: int
    pending_count: int


class AuthorQualitySignal(SQLModel):
    """作者质量通用信号（2.47.0，非表）。

    引擎侧只看这 4 个通用数值；**具体来源由 Provider 决定**——动态 Feed 读
    ``moment_author_quality`` 聚合表映射而来，其它资源类型可由自有画像提供，
    或完全不提供（缺省 0，打分退化为无作者质量项）。
    由此引擎不再依赖任何单一 bizType 的专属表，实现资源无关。
    """

    avg_engagement: float = 0.0
    recent_publish: int = 0
    fans: int = 0
    level: float = 0.0


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


def build_extra_from_stat(stat: TInteractionStat | None) -> tuple[float, float, float]:
    """从 ``TInteractionStat`` **纯 stat 驱动**地派生通用互动特征（2.47.0）。

    返回 ``(engagement, exposure, dislike_ratio)``：

    - ``engagement``：正向互动率 = ``(like+comment+repost+favorite+share+coin) / max(view,1)``
      ——2.47.0 起 ``favorite`` / ``share`` / ``coin`` 一并计入（此前仅 like+comment+repost，
      三个字段被浪费）；防「僵尸爆款」（高曝光低互动）；
    - ``exposure``：曝光量（view），供新鲜度加分（曝光越少越新鲜）；
    - ``dislike_ratio``：点踩占比 = ``dislike / max(dislike+like,1)``，供降权。

    **无资源类型分支**：任意 bizType 只要写了 ``TInteractionStat`` 即可复用本函数，
    这是「用现有资源互动状态特征通用化 Feed」的核心。
    """
    s = stat
    like = int(s.likeCount or 0) if s is not None else 0
    comment = int(s.commentCount or 0) if s is not None else 0
    repost = int(s.repostCount or 0) if s is not None else 0
    favorite = int(s.favoriteCount or 0) if s is not None else 0
    share = int(s.shareCount or 0) if s is not None else 0
    coin = int(s.coinCount or 0) if s is not None else 0
    view = int(s.viewCount or 0) if s is not None else 0
    dislike = int(s.dislikeCount or 0) if s is not None else 0
    positive = like + comment + repost + favorite + share + coin
    engagement = positive / max(view, 1)
    exposure = float(view)
    dislike_ratio = dislike / max(dislike + like, 1)
    return engagement, exposure, dislike_ratio


def _extra(
    c: FeedCandidate,
    stat: TInteractionStat,
    author_quality: dict[int, AuthorQualitySignal] | None,
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
    2.47.0：互动类特征改由 ``build_extra_from_stat`` 纯 stat 驱动（全量互动字段），
    作者质量改收通用 ``AuthorQualitySignal``（引擎不再依赖动态专属表）。

    ``author_quality`` / ``report_counts`` / ``comment_subjects`` 均为
    ``{id: SQLModel 实体}`` 批量查询结果索引（一次 IN 查询后点查，避免 N+1），
    dict 仅作映射容器、值一律为具体实体（2.45.0 起不再出现裸 tuple/dict 值）。
    """
    engagement, exposure, dislike_ratio = build_extra_from_stat(stat)
    extra = EdgeRankExtra(
        engagement=engagement,
        rich=c.rich,
        is_forward=c.is_forward,
        exposure=exposure,
        dislike_ratio=dislike_ratio,
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
        extra.author_quality = aq.avg_engagement
        extra.recent_publish = aq.recent_publish
        extra.fans = aq.fans
        extra.level = float(aq.level)
    if report_counts:
        rc = report_counts.get(c.biz_id)
        if rc:
            extra.report_count = float(rc.pending_count)
    return extra


def build_viewer_key(
    viewer_mid: int | None,
    uniq_id: str | None = None,
) -> str | None:
    """构造曝光去重主体 key（2.47.0）。

    登录用户 ``mid:{mid}``；匿名用户 ``anon:{uniq_id}``。两者皆无（匿名且未带
    ``uniq_id``）→ 返回 None，表示无法标识主体，跳过曝光去重、退化为客户端
    ``last_showlist`` 语义。
    """
    if viewer_mid:
        return f"mid:{viewer_mid}"
    if uniq_id:
        return f"anon:{uniq_id}"
    return None


async def load_impressed_ids(
    session: AsyncSession,
    *,
    viewer_key: str,
    biz_type: InteractionBizTypeEnum,
    feed_scene: str,
    candidate_ids: list[int],
) -> set[int]:
    """读取 TTL 窗口内「该观众在本场景已下发过」的资源 id（曝光去重，2.47.0）。

    一次 IN 查询（非 COUNT 聚合）；无记录返回空集。开关关闭 / TTL<=0 时返回空集。
    """
    if not settings.edgerank_dedup_enabled or not candidate_ids:
        return set()
    ttl_hours = settings.edgerank_dedup_impression_ttl_hours
    if ttl_hours <= 0:
        return set()
    since = datetime.now() - timedelta(hours=ttl_hours)
    ids = candidate_ids[: settings.edgerank_dedup_query_limit]
    rows = (
        await session.exec(
            select(TFeedImpression.bizId).where(
                col(TFeedImpression.viewerKey) == viewer_key,
                col(TFeedImpression.bizType) == biz_type,
                col(TFeedImpression.feedScene) == feed_scene,
                col(TFeedImpression.bizId).in_(ids),
                col(TFeedImpression.lastImpressionAt) >= since,
            )
        )
    ).all()
    return {int(r) for r in rows}


async def record_impressions(
    session: AsyncSession,
    *,
    viewer_key: str,
    biz_type: InteractionBizTypeEnum,
    feed_scene: str,
    biz_ids: list[int],
) -> None:
    """批量记录本次下发的曝光（2.47.0）：单条 ``INSERT ... ON DUPLICATE KEY UPDATE``。

    已曝光过则 ``impressionCount+1`` 并刷新 ``lastImpressionAt``，否则插入新行
    （依赖 ``uq(viewerKey,bizType,bizId,feedScene)``）。

    需**显式 commit**：请求依赖 ``get_session`` 不自动提交（仅异常时回滚），
    若只 flush 则曝光在会话关闭时丢失；Feed 读路径无其它待提交写操作，此处提交安全。
    """
    if not settings.edgerank_dedup_enabled or not biz_ids:
        return
    now = datetime.now()
    values = [
        {
            "viewerKey": viewer_key,
            "bizType": biz_type,
            "bizId": int(bid),
            "feedScene": feed_scene,
            "impressionCount": 1,
            "firstImpressionAt": now,
            "lastImpressionAt": now,
            "created_at": now,
            "updated_at": now,
        }
        for bid in biz_ids
    ]
    stmt = mysql_insert(TFeedImpression).values(values)
    stmt = stmt.on_duplicate_key_update(
        impressionCount=stmt.inserted.impressionCount + 1,
        lastImpressionAt=stmt.inserted.lastImpressionAt,
        updated_at=stmt.inserted.updated_at,
    )
    await session.execute(stmt)
    await session.commit()


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
    author_quality: dict[int, AuthorQualitySignal] | None = None,
    report_counts: dict[int, ResourceReportCount] | None = None,
    comment_subjects: dict[int, CommentSubject] | None = None,
    now: datetime | None = None,
    profile: EdgeRankProfile | None = None,
    viewer_key: str | None = None,
    feed_scene: str | None = None,
    biz_type: InteractionBizTypeEnum | None = None,
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
        page_size: 单页条数；
        profile: 排序权重 Profile 基（缺省 ``FEED_PROFILE``）；匿名随机扰动以本 Profile 为基
          （话题 Feed 传 ``TOPIC_FEED_PROFILE``，使匿名排序基于话题权重）；
        viewer_key: 曝光去重主体（``mid:{mid}`` / ``anon:{uniq_id}``，见
          ``build_viewer_key``）；None → 跳过曝光去重，退化为纯 ``last_showlist``；
        feed_scene: Feed 场景（``comprehensive`` / ``topic`` ...），曝光按场景隔离；
        biz_type: 候选资源 bizType（曝光记录按 bizType 区分）；None → 跳过曝光去重。
    """
    now = now or datetime.now()

    # 2.34.0：未登录不用全局排序——uniq_id 派生随机权重；登录走传入/默认 Profile
    base = profile or FEED_PROFILE
    if viewer_mid is None and settings.edgerank_anon_randomize_enabled:
        prof = build_anon_profile(uniq_id, base=base)
    else:
        prof = base

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
            prof,
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

    # ---- 去重（2.32.0 客户端 last_showlist + 2.47.0 服务端曝光记录）----
    # ``shown``（客户端已展示）是**硬约束**：必须始终排除，绝不因降级而复现；
    # ``impressed``（服务端曝光）为**软约束**：候选因曝光被过滤得不足一页时才放宽。
    shown = set(last_showlist or ())
    always_exclude = [c for c in ranked if c.biz_id not in shown]
    dedup_on = (
        settings.edgerank_dedup_enabled
        and viewer_key is not None
        and feed_scene is not None
        and biz_type is not None
    )
    impressed: set[int] = set()
    if dedup_on:
        impressed = await load_impressed_ids(
            session,
            viewer_key=viewer_key,
            biz_type=biz_type,
            feed_scene=feed_scene,
            candidate_ids=[c.biz_id for c in ranked],
        )

    # 尽力去重 + 自动降级：优先剔除已展示 ∪ 已曝光；若曝光过滤使候选不足一页，
    # 放宽曝光约束（忽略 TTL 内曝光）只按客户端 last_showlist 去重，保证 feed
    # 不被服务端曝光去重掏空——但已展示项永不回填。
    if impressed:
        deduped = [c for c in always_exclude if c.biz_id not in impressed]
        if len(deduped) >= page_size:
            page, has_more = deduped[:page_size], len(deduped) > page_size
        else:
            page, has_more = always_exclude[:page_size], len(always_exclude) > page_size
    else:
        page, has_more = always_exclude[:page_size], len(always_exclude) > page_size

    served_ids = [c.biz_id for c in page]
    if dedup_on and served_ids:
        await record_impressions(
            session,
            viewer_key=viewer_key,
            biz_type=biz_type,
            feed_scene=feed_scene,
            biz_ids=served_ids,
        )
    return served_ids, has_more


__all__ = [
    "AuthorQualitySignal",
    "FeedCandidate",
    "PersonalSignals",
    "ResourceReportCount",
    "build_extra_from_stat",
    "build_viewer_key",
    "load_impressed_ids",
    "load_personal_signals",
    "rank_feed",
    "record_impressions",
]
