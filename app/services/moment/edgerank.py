"""EdgeRank 推荐排序核心模块（2.27.0，计划书决策 #3 落地）。

经典 Facebook EdgeRank 公式的项目内落地：

    score = Σ(w_e · count_e) × decay(age)
    decay(age) = 0.5 ** (age_seconds / half_life_seconds)

- ``w_e``：各互动类型权重（点赞 / 评论 / 转发 / 浏览 / 收藏），可配置（settings.edgerank_*_weights）；
- ``half_life_seconds``：半衰期，越大衰减越慢（偏「热度」），越小越偏「新鲜」；
- ``age``：动态 ``pubTime`` 距今秒数。

三套 Profile（**权重独立，满足「综合 Feed 与话题下 Feed 权重不一样」**）：

- ``FEED_PROFILE``：综合 Feed（``/feed/all?sort=recommend``）——侧重内容质量 + 传播性，
  转发/评论权重高、半衰期 24h；
- ``TOPIC_FEED_PROFILE``：话题 Feed（``/feed/topic/{id}?sort=hot``）——侧重话题讨论氛围 +
  热点时效，评论权重更高、半衰期 6h；
- ``TOPIC_SQUARE_PROFILE``：话题广场 / 热搜——``score = Σ(w·log(count+1))·decay(pubTime)``，
  dynCount/viewCount 用 log 压缩长尾、isHot/sortWeight 直接加权、半衰期 12h。

设计约束：
- **无状态纯函数**：本模块不依赖 DB / IO，便于单测（SQLModel 仅作 typed struct）；
- **计数直接读字段**：调用方传入 ``TInteractionStat`` / 评论计数，禁热路径 COUNT 聚合；
- **降级**：``settings.edgerank_enabled=False`` 时打分退化为「时间倒序等价分」
  （``1e18 - age``），recommend/hot 排序自然回到时间倒序，不影响任何业务行为。
"""

import random
from datetime import datetime, timezone
from math import log

from pydantic import ConfigDict
from sqlmodel import SQLModel

from app.core.config import settings
from app.models.db import TInteractionStat, TMomentTopic
from app.models.enums import InteractionBizTypeEnum

# 零互动时的最小基数分（低于任意「1 个赞」的分值，保证同时刻有互动内容排在前面）
_ZERO_INTERACTION_BASE = 0.1

_MOMENT_WEIGHT_FIELDS = ("like", "comment", "repost", "view", "favorite", "share")


class EdgeRankProfile(SQLModel):
    """EdgeRank 权重配置（非表）。

    动态 Feed 用 ``like/comment/repost/view/favorite/share``；
    话题广场用 ``dynCount/viewCount/isHot/sortWeight``。缺失字段按 0。
    """

    model_config = ConfigDict(extra="ignore")

    like: float = 0.0
    comment: float = 0.0
    repost: float = 0.0
    view: float = 0.0
    favorite: float = 0.0
    share: float = 0.0
    dynCount: float = 0.0
    viewCount: float = 0.0
    isHot: float = 0.0
    sortWeight: float = 0.0
    half_life_seconds: float = 86400.0


class EdgeRankExtra(SQLModel):
    """动态 EdgeRank 附加维度（2.35.0+，非表）。"""

    engagement: float = 0.0
    rich: bool = False
    is_forward: bool = False
    exposure: float = 0.0
    author_quality: float = 0.0
    recent_publish: int = 0
    dislike_ratio: float = 0.0
    report_count: float = 0.0
    fans: int = 0
    level: float = 0.0
    last_activity_time: datetime | None = None


def _empty_stat() -> TInteractionStat:
    return TInteractionStat(bizType=InteractionBizTypeEnum.DYNAMIC, bizId=0)


def _as_naive(dt: datetime | None) -> datetime | None:
    """统一为 naive datetime（服务端 MySQL 读出为 naive UTC，与现有服务一致）。

    若带 tzinfo（TIMESTAMPTZ 场景）先转 UTC 再剥 tz，保证可减。
    """
    if dt is None:
        return None
    if dt.tzinfo is not None:
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _age_seconds(pub_time: datetime | None, now: datetime | None = None) -> float:
    """pub_time 距今秒数（无 pubTime → 正无穷，排最后；非负）。

    ``now`` 默认取 ``datetime.now()``（本地 CST）——与业务写入（``moment_publish``
    的 ``datetime.now()`` / biliopusdb 真实数据 / 数据库 ``NOW()``）同一时间基准，
    避免 UTC/CST 字面值 8 小时错位导致 decay/候选窗口失真。
    """
    p = _as_naive(pub_time)
    if p is None:
        return float("inf")
    now = _as_naive(now) if now is not None else datetime.now()
    return max(0.0, (now - p).total_seconds())


def decay(age_seconds: float, half_life_seconds: float) -> float:
    """指数时间衰减：age=0 → 1.0；age=half_life → 0.5；age→∞ → 0。

    非法入参（负半衰期等）按无衰减处理（返回 1.0），保证永不崩。
    """
    if age_seconds <= 0 or half_life_seconds <= 0:
        return 1.0
    return 0.5 ** (age_seconds / half_life_seconds)


def build_moment_counts(
    stat: TInteractionStat | None,
    comment_count_override: int | None = None,
) -> TInteractionStat:
    """从 ``TInteractionStat`` 拷贝一份打分用计数（评论系统实时计数优先覆盖 commentCount）。

    直接读字段，禁 COUNT 聚合；返回脱离 session 的新实例，避免改 commentCount 脏 ORM。
    """
    comment = (
        int(comment_count_override)
        if comment_count_override is not None
        else int((stat.commentCount if stat is not None else 0) or 0)
    )
    if stat is None:
        out = _empty_stat()
        out.commentCount = comment
        return out
    return TInteractionStat(
        bizType=stat.bizType,
        bizId=stat.bizId,
        likeCount=int(stat.likeCount or 0),
        commentCount=comment,
        repostCount=int(stat.repostCount or 0),
        viewCount=int(stat.viewCount or 0),
        favoriteCount=int(stat.favoriteCount or 0),
        shareCount=int(stat.shareCount or 0),
        dislikeCount=int(stat.dislikeCount or 0),
        coinCount=int(stat.coinCount or 0),
    )


def _weighted_interactions(stat: TInteractionStat, profile: EdgeRankProfile) -> float:
    return (
        profile.like * int(stat.likeCount or 0)
        + profile.comment * int(stat.commentCount or 0)
        + profile.repost * int(stat.repostCount or 0)
        + profile.view * int(stat.viewCount or 0)
        + profile.favorite * int(stat.favoriteCount or 0)
        + profile.share * int(stat.shareCount or 0)
    )


def compute_moment_score(
    stat: TInteractionStat | None,
    pub_time: datetime | None,
    profile: EdgeRankProfile,
    *,
    now: datetime | None = None,
    enabled: bool | None = None,
    extra: EdgeRankExtra | None = None,
) -> float:
    """动态 EdgeRank 分数（2.35.0 多维）。

    - ``stat``：``TInteractionStat`` 计数（None 按全 0）；
    - ``pub_time``：动态 pubTime（时间衰减基准）；
    - ``profile``：权重 Profile（综合 Feed / 话题 Feed 各自传入）；
    - ``enabled=None`` 时取 ``settings.edgerank_enabled``；
    - ``extra``：附加维度（互动率 / 丰富度 / 转发惩罚 / 曝光 / 作者质量 / 刷屏 /
      点踩 / 举报 / 粉丝等级 / 最近活跃时间）。

    零互动内容退化为「最小基数 × decay」（约 0~1），排在互动内容之后、
    零互动之间按时间排序；``edgerank_enabled=False`` 时返回时间倒序等价分。
    """
    if enabled is None:
        enabled = settings.edgerank_enabled
    if not enabled:
        return 1e18 - _age_seconds(pub_time, now)

    counts = stat if stat is not None else _empty_stat()
    extra = extra or EdgeRankExtra()
    weighted = _weighted_interactions(counts, profile)
    # 2.43.0：时间衰减基准优先用「最近活跃时间」（最后评论时间），否则回退发布时间
    age_ref = extra.last_activity_time or pub_time
    d = decay(_age_seconds(age_ref, now), profile.half_life_seconds)
    score = weighted * d if weighted > 0 else _ZERO_INTERACTION_BASE * d
    score += settings.edgerank_engagement_weight * extra.engagement
    score += settings.edgerank_rich_weight * (1.0 if extra.rich else 0.0)
    score += settings.edgerank_forward_penalty * (1.0 if extra.is_forward else 0.0)
    score += settings.edgerank_fresh_weight / (1.0 + float(extra.exposure))
    score += settings.edgerank_author_quality_weight * extra.author_quality
    threshold = settings.edgerank_author_publish_threshold
    if extra.recent_publish > threshold:
        score -= settings.edgerank_author_spam_penalty * (extra.recent_publish - threshold)
    score -= settings.edgerank_dislike_penalty * extra.dislike_ratio
    score -= settings.edgerank_report_penalty * extra.report_count
    score += settings.edgerank_fans_weight * log(1 + int(extra.fans))
    score += settings.edgerank_level_weight * extra.level
    return score


def compute_topic_score(
    topic: TMomentTopic | None,
    profile: EdgeRankProfile,
    *,
    now: datetime | None = None,
    enabled: bool | None = None,
) -> float:
    """话题广场 EdgeRank 分数（``score = Σ(w·log(count+1))·decay(pubTime)``）。

    - ``dynCount`` / ``viewCount``：log 压缩长尾（0 → 0，1 → log2，10 → log11 …）；
    - ``isHot`` / ``sortWeight``：直接加权（运营可干预）；
    - ``pubTime``：时间衰减（越早越靠后）；``pubTime=None`` 视为正无穷老 → 排最后。
    """
    if enabled is None:
        enabled = settings.edgerank_enabled
    if not enabled or topic is None:
        return 1e18 - _age_seconds(topic.pubTime if topic is not None else None, now)

    total = (
        profile.dynCount * log(int(topic.dynCount or 0) + 1)
        + profile.viewCount * log(int(topic.viewCount or 0) + 1)
        + profile.isHot * int(topic.isHot or 0)
        + profile.sortWeight * int(topic.sortWeight or 0)
    )
    return total * decay(_age_seconds(topic.pubTime, now), profile.half_life_seconds)


def _profile_from_settings(weights: dict[str, float], half_life_seconds: float) -> EdgeRankProfile:
    """dict 配置桥接（pydantic-settings JSON 权重 → ``EdgeRankProfile``，2.45.0 关键字直构）。"""
    return EdgeRankProfile(**weights, half_life_seconds=float(half_life_seconds))


# ==================== 三套 Profile（权重独立，可配置） ====================

FEED_PROFILE = _profile_from_settings(
    settings.edgerank_feed_weights,
    float(settings.edgerank_feed_half_life_seconds),
)

TOPIC_FEED_PROFILE = _profile_from_settings(
    settings.edgerank_topic_feed_weights,
    float(settings.edgerank_topic_feed_half_life_seconds),
)

TOPIC_SQUARE_PROFILE = _profile_from_settings(
    settings.edgerank_topic_square_weights,
    float(settings.edgerank_topic_square_half_life_seconds),
)


def build_anon_profile(
    seed: str | int | None,
    *,
    ratio: float | None = None,
) -> EdgeRankProfile:
    """基于 ``FEED_PROFILE`` 随机扰动生成匿名推荐 Profile（2.34.0；2.45.0 去 dict 中间态）。

    未登录用户不以全局排序返回：以 ``seed``（如客户端 ``uniq_id``）为随机种子，
    对每项动态权重乘 ``[1-ratio, 1+ratio]`` 的确定性扰动（``random.Random(seed)``），
    使不同匿名用户/会话在分数相近内容间看到不同顺序（整体仍以热度为基调）。
    ``seed`` 为空时每次调用随机；``ratio`` 缺省取
    ``settings.edgerank_anon_perturb_ratio``。
    """
    r = settings.edgerank_anon_perturb_ratio if ratio is None else ratio
    rng = random.Random(seed)
    profile = FEED_PROFILE.model_copy()
    for field in _MOMENT_WEIGHT_FIELDS:
        setattr(
            profile,
            field,
            float(getattr(profile, field)) * (1 + rng.uniform(-r, r)),
        )
    return profile


__all__ = [
    "FEED_PROFILE",
    "TOPIC_FEED_PROFILE",
    "TOPIC_SQUARE_PROFILE",
    "EdgeRankExtra",
    "EdgeRankProfile",
    "build_anon_profile",
    "build_moment_counts",
    "compute_moment_score",
    "compute_topic_score",
    "decay",
]
