"""通用资源 Feed 引擎（2.36.0，承载 2.35.0 全量排序信号）。

任意资源（``bizType+bizId``）经 Provider 提供候选与计数后，统一由本引擎执行
推荐流水线（与资源类型无关）：

1. **EdgeRank 多维打分**（2.35.0；2.47.0 全量互动特征）：``compute_moment_score(stat, pubTime, profile, extra)``
   ——计数用 ``TInteractionStat``，附加维度用 ``EdgeRankExtra``（2.47.0 起 like/comment/
   repost/**favorite/share/coin**/view/dislike 全量参与特征构造，纯 stat 驱动、无资源类型分支）；
2. **个性化 boost（登录，2.33.0 + 2.35.0 反馈闭环）**：关注作者 / 点赞过作者 /
   ``last_clicklist`` 点击作者 / 偏好话题 / 点击话题；
2.1 **点踩个人化降权（登录，2.62.0，计划书 §5.20）**：**对点踩者本人大幅降权**
   ——命中「我点踩过」的候选 → ``score = score · scale − weight``，与正向 boost 对称；
   全局「对所有人略降」仍在 ``edgerank`` 的 ``dislike_ratio`` 里（2.35.0），本引擎不含该分支，
   以保证算法层无 IO；可选 ``edgerank_personal_dislike_exclude`` 直接剔除点踩过的资源；
3. **匿名随机权重（未登录，2.34.0）**：以 ``uniq_id`` 为种子派生扰动 Profile；
4. **去重（2.32.0 + 2.47.0 曝光去重）**：排除客户端 ``last_showlist`` ∪ 服务端
   ``TFeedImpression``（该观众在本场景 TTL 内已下发过的资源）；**尽力去重 + 自动降级**
   ——候选不足一页时逐级放宽，保证 feed 不因去重见底；
4.5 **重排（2.65.0，业界「召回→粗排→精排→重排」链路的最后一环）**：``Thompson
   Sampling`` 探索加分（``θ ~ Beta(1+pos, 1+neg)``，低曝光内容有冒头机会、随真实
   反馈自动收敛）+ ``MMR`` 多样性打散（``λ·rel − (1−λ)·max sim``，防同作者/同话题
   刷屏）；只调顺序不改集合；
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

import random
from datetime import datetime, timedelta

from bili_common.models import InteractionBizTypeEnum
from pydantic import ConfigDict
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlmodel import Field, SQLModel, col, select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.core.config import settings
from app.models.db import (
    CommentSubject,
    TFeedBanditArm,
    TFeedImpression,
    TInteractionStat,
    TMoment,
    TMomentTopicRel,
    TResourceDislike,
    TResourceLike,
)
from app.models.schemas.sys_config import FeedMLConfig
from app.services.common.indexed import Indexed
from app.services.moment.edgerank import (
    FEED_PROFILE,
    EdgeRankExtra,
    EdgeRankProfile,
    build_anon_profile,
    build_moment_counts,
    compute_moment_score,
)

# 2.66.0：曝光流水写入（模型训练样本源）
from app.services.moment.feed_event import FeedEventService
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
    # 2.66.0：正文长度（ML 精排特征；由 Provider 从内容行装配，缺则 0）
    text_len: int = 0


class PersonalSignals(SQLModel):
    """登录用户个性化信号（2.33.0 + 2.35.0；2.62.0 新增负反馈 `disliked`）。"""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    follow: set[int] = Field(default_factory=set)
    liked_author: set[int] = Field(default_factory=set)
    topic: set[int] = Field(default_factory=set)
    clicked_author: set[int] = Field(default_factory=set)
    clicked_topic: set[int] = Field(default_factory=set)
    # 2.62.0：该观众点踩过的资源 bizId（负反馈信号，供「对点踩者本人大幅降权」）
    disliked: set[int] = Field(default_factory=set)


async def load_personal_signals(
    session: AsyncSession,
    viewer_mid: int,
    clicklist: list[int] | None = None,
    biz_type: InteractionBizTypeEnum = InteractionBizTypeEnum.DYNAMIC,
    candidate_ids: list[int] | None = None,
) -> PersonalSignals:
    """加载推荐流个性化信号（2.33.0 + 2.35.0 反馈闭环；2.62.0 负反馈）。

    - ``follow``：关注作者（``msg_user_follow``）；
    - ``liked_author`` / ``topic``：最近 ``edgerank_personalized_like_history_limit``
      条点赞历史（``TResourceLike``）批量 join ``TMoment`` / ``TMomentTopicRel``；
    - ``clicked_author`` / ``clicked_topic``：``last_clicklist``（客户端已互动列表）
      对应作者/话题（2.35.0 反馈闭环）；
    - ``disliked``：该观众点踩过的资源 bizId（``TResourceDislike``，2.62.0 负反馈信号）。
      **只查候选范围内**的记录（``bizId IN candidate_ids``，一次 IN 查询），
      不加载全部点踩历史；与正向 boost 对称地供「对点踩者本人大幅降权」使用。

    任一信号为空不影响其余（打分时缺省按 0）。
    """
    signals = PersonalSignals()

    # 2.62.0：点踩明细（负反馈）——按候选 id 收窄，查询成本与页面大小相关而非用户历史长度
    if settings.edgerank_personal_dislike_enabled and candidate_ids:
        dis_rows = (
            await session.exec(
                select(TResourceDislike.bizId).where(
                    col(TResourceDislike.bizType) == biz_type,
                    col(TResourceDislike.mid) == viewer_mid,
                    col(TResourceDislike.bizId).in_(candidate_ids),
                )
            )
        ).all()
        signals.disliked = {int(r) for r in dis_rows if r is not None}

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


def apply_dislike_penalty(
    score: float, c: FeedCandidate, signals: PersonalSignals
) -> float:
    """点踩者本人大幅降权（2.62.0，计划书 §5.20）：命中「我点踩过」的候选 → ``s·scale − weight``。

    与 :func:`_boost`（正向个性化）对称，同在精排后处理，保证 ``edgerank`` 算法层保持
    **无 IO 纯函数**（不含任何 per-user 分支）。

    - ``scale``（``edgerank_personal_dislike_scale``）：比例压制，高分内容同样显著下沉；
    - ``weight``（``edgerank_personal_dislike_weight``）：固定扣分，中等分数内容直接沉底。

    注意：点踩**不改变内容可见性**（只降权不下架）——下架仍由举报审核 ``resourceAction=hide``
    独占（2.38.0）。开关关闭 / 未命中点踩时原分返回。
    """
    if not settings.edgerank_personal_dislike_enabled:
        return score
    if c.biz_id not in signals.disliked:
        return score
    return (
        score * settings.edgerank_personal_dislike_scale
        - settings.edgerank_personal_dislike_weight
    )


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


def _stat_positive_negative(stat: TInteractionStat | None) -> tuple[float, float]:
    """从 ``TInteractionStat`` 拆出「正反馈 / 未互动曝光」两计数（TS 探索用，2.65.0）。

    - ``positive`` = like+comment+repost+favorite+share+coin（一次曝光产生了互动）；
    - ``negative`` = max(view − positive, 0)（曝光了但没互动，视为「失败」）。
    """
    if stat is None:
        return 0.0, 0.0

    def _v(attr: str) -> float:
        return float(int(getattr(stat, attr) or 0))

    positive = sum(
        _v(f)
        for f in (
            "likeCount",
            "commentCount",
            "repostCount",
            "favoriteCount",
            "shareCount",
            "coinCount",
        )
    )
    return positive, max(_v("viewCount") - positive, 0.0)


def thompson_exploration_bonus(
    stat: TInteractionStat | None,
    *,
    biz_id: int,
    base_score: float,
    seed: str | None = None,
) -> float:
    """Thompson Sampling 探索加分（2.65.0）：``w · base · (θ − E[θ])``，``θ ~ Beta(α, β)``。

    业界标准的**探索-利用（EE）**做法（多臂老虎机 / Beta-Bernoulli 共轭）：把「这条
    内容被曝光后产生互动的概率」当作未知参数 θ，用 Beta 后验描述它——

    - **数据少**（新内容 / 低曝光）：后验分布宽，采样值波动大 → 有机会被探索到前面；
    - **数据多**（老爆款）：后验收敛到真实互动率 → 波动小 → 稳定利用。

    由此**不依赖时间窗口**也能让「没人看过的好内容」冒头，且随真实反馈自动收敛
    （不是瞎随机）。计数按 ``edgerank_ts_max_count`` 截断（压缩长尾），避免千万级
    曝光的爆款把分布彻底锁死、永远失去探索。

    加分形如 ``w · base · (θ − E[θ])``（**相对扰动**，非绝对加分）：

    - 减 ``E[θ] = α/(α+β)`` 使扰动零均值——不改变期望排序，只注入探索噪声；
    - 乘 ``base`` 使扰动与内容分同量级：零互动 / 已沉底的内容（``base<=0``）不参与
      探索，避免「全库零互动时 Feed 变成随机排序」；
    - 爆款因后验收敛，实际波动远小于冷内容，正是 EE 想要的「利用 vs 探索」。

    采样以 ``(seed, biz_id)`` 为种子**确定性派生**：同一观众刷新页面不会看到排序乱跳
    （与 ``build_anon_profile`` 的稳定性约定一致）。
    """
    if not settings.edgerank_ts_enabled or base_score <= 0:
        return 0.0
    pos, neg = _stat_positive_negative(stat)
    # 零曝光（pos=neg=0）没有后验可言：θ 退化成先验 U(0,1)，纯噪声会盖掉时间序，
    # 故不参与探索——这类内容按精排分（新鲜度 / 时间衰减）自然排序即可。
    if pos + neg <= 0:
        return 0.0
    cap = settings.edgerank_ts_max_count
    alpha = settings.edgerank_ts_prior_alpha + min(pos, cap)
    beta = settings.edgerank_ts_prior_beta + min(neg, cap)
    if alpha <= 0 or beta <= 0:
        return 0.0
    rng = random.Random(f"ts:{seed or 'global'}:{biz_id}")
    theta = rng.betavariate(alpha, beta)
    return settings.edgerank_ts_weight * base_score * (theta - alpha / (alpha + beta))


def apply_ml_ranker(
    ranked: list[FeedCandidate],
    score_map: dict[int, float],
    *,
    counts: Indexed[TInteractionStat],
    author_quality: Indexed[AuthorQualitySignal] | None,
    signals: PersonalSignals | None,
    now: datetime,
    config: FeedMLConfig | None = None,
) -> list[FeedCandidate]:
    """2.66.0：把 LightGBM 精排分融合进 EdgeRank 分并重排（模型不可用则原序）。

    特征与离线 ``scripts/build_feed_samples.py`` 的 ``FEATURE_NAMES`` 同序（由
    :func:`~app.services.moment.ml_ranker.to_vector` 保证）。**``position`` 恒传 0**：
    位置偏差是曝光后才有的信息，在线让它参与打分会自我强化「先下发的一定排前面」。
    """
    from app.services.moment.ml_ranker import (
        CandidateFeatures,
        RankContext,
        score_candidates,
    )

    follow = signals.follow if signals is not None else set()
    pref_topics = signals.topic if signals is not None else set()

    features: list[CandidateFeatures] = []
    for c in ranked:
        stat = counts.value_of(c.biz_id)
        aq = (
            author_quality.value_of(c.mid)
            if author_quality is not None and c.mid is not None
            else None
        )
        features.append(
            CandidateFeatures(
                biz_id=c.biz_id,
                mid=c.mid or 0,
                pub_time=c.pub_time,
                topic_count=len(c.tags or ()),
                is_rich=c.rich,
                is_forward=c.is_forward,
                text_len=c.text_len,
                like_count=int(stat.likeCount or 0) if stat is not None else 0,
                comment_count=int(stat.commentCount or 0) if stat is not None else 0,
                repost_count=int(stat.repostCount or 0) if stat is not None else 0,
                view_count=int(stat.viewCount or 0) if stat is not None else 0,
                author_avg_engagement=aq.avg_engagement if aq is not None else 0.0,
                author_recent_publish=aq.recent_publish if aq is not None else 0,
                author_fans=aq.fans if aq is not None else 0,
                author_level=aq.level if aq is not None else 0.0,
                is_followed=c.mid is not None and c.mid in follow,
                topic_hit=len(set(c.tags or ()) & pref_topics),
                position=0,
            )
        )

    finals = score_candidates(
        features,
        [score_map[c.biz_id] for c in ranked],
        RankContext(now=now, hour_of_day=now.hour),
    )
    for c, final in zip(ranked, finals, strict=False):
        score_map[c.biz_id] = final
    return sorted(ranked, key=lambda c: score_map[c.biz_id], reverse=True)


async def apply_linucb(
    session,
    ranked: list[FeedCandidate],
    score_map: dict[int, float],
    *,
    counts: Indexed[TInteractionStat],
    author_quality: Indexed[AuthorQualitySignal] | None,
    signals: PersonalSignals | None,
    biz_type: InteractionBizTypeEnum,
    config: FeedMLConfig | None = None,
) -> list[FeedCandidate]:
    """2.66.0：LinUCB 探索分叠加（带上下文特征的 bandit，替代无特征 TS 的泛化短板）。

    臂参数（A/b）一次 IN 查询后点查；冷启动（无臂行）退化为「纯探索」——
    ``ucb = α·‖x‖``，即特征强的内容先拿到曝光机会。
    """
    from app.services.moment.linucb import (
        ArmContext,
        blend_ucb,
        context_vector,
        load_arms,
        ucb_score,
    )

    ids = [c.biz_id for c in ranked][: settings.edgerank_dedup_query_limit]
    if not ids:
        return ranked
    rows = (
        await session.exec(
            select(TFeedBanditArm).where(
                col(TFeedBanditArm.bizType) == biz_type,
                col(TFeedBanditArm.bizId).in_(ids),
            )
        )
    ).all()
    arms = load_arms(rows)
    follow = signals.follow if signals is not None else set()
    pref_topics = signals.topic if signals is not None else set()

    ucb_scores: list[float] = []
    for c in ranked:
        stat = counts.value_of(c.biz_id)
        view = int(stat.viewCount or 0) if stat is not None else 0
        positive = (
            sum(
                int(getattr(stat, f) or 0)
                for f in (
                    "likeCount",
                    "commentCount",
                    "repostCount",
                    "favoriteCount",
                    "shareCount",
                    "coinCount",
                )
            )
            if stat is not None
            else 0
        )
        aq = (
            author_quality.value_of(c.mid)
            if author_quality is not None and c.mid is not None
            else None
        )
        age_hours = (
            (datetime.now() - c.pub_time).total_seconds() / 3600.0
            if c.pub_time is not None
            else 0.0
        )
        x = context_vector(
            ArmContext(
                is_rich=c.rich,
                is_forward=c.is_forward,
                topic_count=len(c.tags or ()),
                text_len=c.text_len,
                age_hours=age_hours,
                engagement=positive / max(view, 1),
                author_quality=aq.avg_engagement if aq is not None else 0.0,
                is_followed=c.mid is not None and c.mid in follow,
                topic_hit=len(set(c.tags or ()) & pref_topics),
            )
        )
        a, b = arms.get(c.biz_id, (None, None))
        ucb_scores.append(
            ucb_score(
                x,
                a_matrix=a,
                b_vector=b,
                alpha=config.linucb_alpha if config is not None else None,
            )
        )

    finals = blend_ucb(
        [score_map[c.biz_id] for c in ranked],
        ucb_scores,
        weight=config.linucb_weight if config is not None else None,
    )
    for c, final in zip(ranked, finals, strict=False):
        score_map[c.biz_id] = final
    return sorted(ranked, key=lambda c: score_map[c.biz_id], reverse=True)


def mmr_rerank(
    candidates: list[FeedCandidate],
    scores: dict[int, float],
    *,
    page_size: int,
) -> list[FeedCandidate]:
    """MMR 多样性重排（2.65.0）：``MR_i = λ·rel_i − (1−λ)·max_{j∈S} sim(i,j)``。

    业界重排层标配（Maximal Marginal Relevance）：精排分出 top-K 后贪心挑选——每一步
    挑「相关性高、且与已选集合最不相似」的一条，避免整页被同一作者 / 同一话题刷屏。

    - ``rel_i``：精排分在本批候选内做 min-max 归一化到 [0,1]（与相似度可加）；
    - ``sim(i,j)``：同作者（``w_author``）+ 话题 Jaccard 重合（``w_topic``）；
    - ``λ``：1=纯按分排，0=纯多样性；默认 0.85（轻微打散，保住相关性）。

    只对前 ``edgerank_mmr_top_k`` 条重排（性能可控），其余保持精排顺序接在后面。
    """
    if not settings.edgerank_mmr_enabled or len(candidates) <= 1:
        return candidates
    top_k = settings.edgerank_mmr_top_k
    head = candidates[:top_k] if top_k > 0 else candidates
    tail = candidates[len(head) :]

    vals = [scores.get(c.biz_id, 0.0) for c in head]
    lo, hi = min(vals), max(vals)
    span = hi - lo
    lam = settings.edgerank_mmr_lambda
    w_author = settings.edgerank_mmr_author_sim
    w_topic = settings.edgerank_mmr_topic_sim
    mids = [c.mid for c in head]
    tag_sets = [set(c.tags or ()) for c in head]
    rels = [((v - lo) / span) if span > 0 else 1.0 for v in vals]

    def _sim(i: int, j: int) -> float:
        sim = w_author if (mids[i] is not None and mids[i] == mids[j]) else 0.0
        a, b = tag_sets[i], tag_sets[j]
        if a and b:
            sim += w_topic * (len(a & b) / len(a | b))
        return sim

    selected: list[int] = []
    remaining = list(range(len(head)))
    while remaining:
        best_i = -1
        best_mr = float("-inf")
        for i in remaining:
            max_sim = 0.0
            for j in selected:
                s_ij = _sim(i, j)
                if s_ij > max_sim:
                    max_sim = s_ij
            mr = lam * rels[i] - (1.0 - lam) * max_sim
            if mr > best_mr:
                best_i, best_mr = i, mr
        selected.append(best_i)
        remaining.remove(best_i)
    return [head[i] for i in selected] + tail


def _extra(
    c: FeedCandidate,
    stat: TInteractionStat,
    author_quality: Indexed[AuthorQualitySignal] | None,
    report_counts: Indexed[ResourceReportCount] | None = None,
    *,
    comment_subjects: Indexed[CommentSubject] | None = None,
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
    批量查询结果的 `Indexed[实体]`（一次 IN 查询后点查，避免 N+1）；
    值一律为具体实体，未命中返回 None（不再用裸 dict 承载映射）。
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
        cs = comment_subjects.value_of(c.biz_id)
        last_at = cs.updated_at if cs is not None else None
        if last_at is not None:
            extra.last_activity_time = (
                max(c.pub_time, last_at) if c.pub_time is not None else last_at
            )
    aq = (
        author_quality.value_of(c.mid) if author_quality and c.mid is not None else None
    )
    if aq:
        extra.author_quality = aq.avg_engagement
        extra.recent_publish = aq.recent_publish
        extra.fans = aq.fans
        extra.level = float(aq.level)
    if report_counts:
        rc = report_counts.value_of(c.biz_id)
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
    counts: Indexed[TInteractionStat],
    *,
    viewer_mid: int | None,
    last_showlist: list[int] | None,
    uniq_id: str | None,
    page_size: int,
    clicklist: list[int] | None = None,
    author_quality: Indexed[AuthorQualitySignal] | None = None,
    report_counts: Indexed[ResourceReportCount] | None = None,
    comment_subjects: Indexed[CommentSubject] | None = None,
    now: datetime | None = None,
    profile: EdgeRankProfile | None = None,
    viewer_key: str | None = None,
    feed_scene: str | None = None,
    biz_type: InteractionBizTypeEnum | None = None,
    ml_config: FeedMLConfig | None = None,
) -> tuple[list[int], bool]:
    """通用推荐流水线，返回 ``(排序后的 biz_id 列表, has_more)``。

    Args:
        candidates: Provider 提供的候选（已按 bizType 过滤、含 pubTime/tags/特征）；
        counts: `Indexed[TInteractionStat]`（biz_id 点查计数行）；
        clicklist: 客户端已互动 biz_id 列表（2.35.0 反馈闭环）；
        author_quality: `Indexed[AuthorQualitySignal]`（作者质量表，仅 dynamic 等有聚合的
          bizType 提供；读 ``avgEngagement`` / ``recentPublishCount`` /
          ``fansCount`` / ``currentLevel``）；
        report_counts: `Indexed[ResourceReportCount]`（pending 举报数，全资源通用降权）；
        comment_subjects: `Indexed[CommentSubject]`（``root_count`` 实时计数与
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

    # 2.33.0 + 2.35.0：个性化信号（登录 + 开关开启）；2.62.0 附带点踩负反馈集合
    personalized = viewer_mid is not None and settings.edgerank_personalized_enabled
    signals: PersonalSignals | None = None
    if personalized:
        signals = await load_personal_signals(
            session,
            viewer_mid,
            clicklist,
            biz_type=biz_type or InteractionBizTypeEnum.DYNAMIC,
            candidate_ids=[c.biz_id for c in candidates],
        )
        # 2.62.0：可选硬约束——点踩过的资源直接剔除出该观众 Feed（默认关闭，见 §5.20 风控）
        if signals.disliked and settings.edgerank_personal_dislike_exclude:
            candidates = [c for c in candidates if c.biz_id not in signals.disliked]

    # 2.65.0 重排层：TS 探索与 MMR 打散共用同一观众种子（刷新不跳变）
    seed = viewer_key or uniq_id
    score_map: dict[int, float] = {}

    def _score(c: FeedCandidate) -> float:
        cs = comment_subjects.value_of(c.biz_id) if comment_subjects else None
        cm = build_moment_counts(
            counts.value_of(c.biz_id), cs.root_count if cs is not None else None
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
        # 2.65.0：Thompson Sampling 探索加分（EE：低曝光内容有机会冒头）
        s += thompson_exploration_bonus(cm, biz_id=c.biz_id, seed=seed, base_score=s)
        if personalized and signals is not None:
            s += _boost(c, signals)
            # 2.62.0：点踩者本人大幅降权（负反馈，与正向 boost 对称，只对本人生效）
            s = apply_dislike_penalty(s, c, signals)
        score_map[c.biz_id] = s
        return s

    ranked = sorted(candidates, key=_score, reverse=True)

    # ---- 2.66.0：ML 精排融合（LightGBM 与 EdgeRank 分加权，模型不可用则原序）----
    # 开关/权重来自运行时配置 ``feed_ml``（管理端可热更新）；未传入时回落 settings。
    ml_cfg = ml_config or FeedMLConfig(
        ranker_enabled=settings.ml_ranker_enabled,
        ranker_blend_weight=settings.ml_ranker_blend_weight,
        linucb_enabled=settings.ml_linucb_enabled,
        linucb_weight=settings.ml_linucb_weight,
        linucb_alpha=settings.ml_linucb_alpha,
    )
    if ml_cfg.ranker_enabled:
        ranked = apply_ml_ranker(
            ranked,
            score_map,
            counts=counts,
            author_quality=author_quality,
            signals=signals,
            now=now,
            config=ml_cfg,
        )
    if ml_cfg.linucb_enabled:
        ranked = await apply_linucb(
            session,
            ranked,
            score_map,
            counts=counts,
            author_quality=author_quality,
            signals=signals,
            biz_type=biz_type or InteractionBizTypeEnum.DYNAMIC,
            config=ml_cfg,
        )

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
        pool = deduped if len(deduped) >= page_size else always_exclude
    else:
        pool = always_exclude

    # ---- 重排层（2.65.0）：MMR 多样性打散（只调顺序，不改集合）----
    ordered = (
        mmr_rerank(pool, score_map, page_size=page_size)
        if settings.edgerank_rerank_enabled
        else pool
    )
    page, has_more = ordered[:page_size], len(ordered) > page_size

    served_ids = [c.biz_id for c in page]
    if dedup_on and served_ids:
        await record_impressions(
            session,
            viewer_key=viewer_key,
            biz_type=biz_type,
            feed_scene=feed_scene,
            biz_ids=served_ids,
        )
        # 2.66.0：曝光流水（模型训练样本源；与去重表职责分离，弱依赖）
        if settings.ml_event_enabled:
            await FeedEventService.record_impressions(
                session,
                viewer_key=viewer_key,
                biz_type=biz_type,
                feed_scene=feed_scene,
                biz_ids=served_ids,
                scores=Indexed.of(
                    (bid, score_map[bid]) for bid in served_ids if bid in score_map
                ),
                now=now,
            )
    return served_ids, has_more


__all__ = [
    "AuthorQualitySignal",
    "FeedCandidate",
    "PersonalSignals",
    "ResourceReportCount",
    "apply_dislike_penalty",
    "apply_ml_ranker",
    "build_extra_from_stat",
    "build_viewer_key",
    "load_impressed_ids",
    "load_personal_signals",
    "mmr_rerank",
    "rank_feed",
    "record_impressions",
    "thompson_exploration_bonus",
]
