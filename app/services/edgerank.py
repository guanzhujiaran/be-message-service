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
- **无状态纯函数**：本模块不依赖 DB / IO，便于单测；
- **计数直接读字段**：调用方传入 ``TMomentStat`` / 评论计数，禁热路径 COUNT 聚合；
- **降级**：``settings.edgerank_enabled=False`` 时打分退化为「时间倒序等价分」
  （``1e18 - age``），recommend/hot 排序自然回到时间倒序，不影响任何业务行为。
"""

import random
from dataclasses import dataclass, field
from datetime import datetime, timezone
from math import log
from typing import Any

from app.core.config import settings

# 互动计数键（与 TMomentStat 字段 / 评论系统计数对齐）
LIKE = "like"
COMMENT = "comment"
REPOST = "repost"
VIEW = "view"
FAVORITE = "favorite"
SHARE = "share"

# 话题广场计数键（与 TMomentTopic 字段对齐）
TOPIC_DYN_COUNT = "dynCount"
TOPIC_VIEW_COUNT = "viewCount"
TOPIC_IS_HOT = "isHot"
TOPIC_SORT_WEIGHT = "sortWeight"

# 零互动时的最小基数分（低于任意「1 个赞」的分值，保证同时刻有互动内容排在前面）
_ZERO_INTERACTION_BASE = 0.1


@dataclass(frozen=True)
class EdgeRankProfile:
    """EdgeRank 权重配置（不可变）。

    ``weights``：{互动键: 权重}，缺失键按 0 计；
    ``half_life_seconds``：指数衰减半衰期。
    """

    weights: dict[str, float] = field(default_factory=dict)
    half_life_seconds: float = 86400.0

    def weight(self, key: str) -> float:
        return float(self.weights.get(key, 0.0))


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
    stat: Any | None,
    comment_count_override: int | None = None,
) -> dict[str, int]:
    """从统计（ORM 对象 / dict / None）组装 {like, comment, repost, view, favorite, share} 计数。

    2.36.0 通用化：``stat`` 可为 ``TInteractionStat`` 对象、计数 dict
    （小写键 ``{like, comment, ...}``，通用引擎 Provider 输入），或 None。
    直接读字段，禁 COUNT 聚合；``comment_count_override``：评论系统实时计数优先。
    """
    if stat is None:
        stat = {}
    if isinstance(stat, dict):
        like = int(stat.get("like", 0))
        comment = int(stat.get("comment", 0))
        repost = int(stat.get("repost", 0))
        view = int(stat.get("view", 0))
        favorite = int(stat.get("favorite", 0))
        share = int(stat.get("share", 0))
    else:
        like = int(stat.likeCount or 0)
        comment = int(stat.commentCount or 0)
        repost = int(stat.repostCount or 0)
        view = int(stat.viewCount or 0)
        favorite = int(stat.favoriteCount or 0)
        share = int(stat.shareCount or 0)
    if comment_count_override is not None:
        comment = int(comment_count_override)
    return {
        LIKE: like,
        COMMENT: comment,
        REPOST: repost,
        VIEW: view,
        FAVORITE: favorite,
        SHARE: share,
    }


def compute_moment_score(
    counts: dict[str, int],
    pub_time: datetime | None,
    profile: EdgeRankProfile,
    *,
    now: datetime | None = None,
    enabled: bool | None = None,
    extra: dict[str, Any] | None = None,
) -> float:
    """动态 EdgeRank 分数（2.35.0 多维）。

    - ``counts``：{like, comment, repost, view, favorite, share} 计数（缺省按 0）；
    - ``pub_time``：动态 pubTime（时间衰减基准）；
    - ``profile``：权重 Profile（综合 Feed / 话题 Feed 各自传入）；
    - ``enabled=None`` 时取 ``settings.edgerank_enabled``；
    - ``extra``（可选）：2.35.0 附加维度信号——``engagement``（互动率）、
      ``rich``（内容丰富度）、``is_forward``（转发惩罚）、``exposure``（曝光量，
      fresh 冷启动）、``author_quality``（作者质量）、``recent_publish``（近 7 天
      发布量，超阈值刷屏惩罚）、``dislike_ratio``（点踩比例降权）。

    零互动内容退化为「最小基数 × decay」（约 0~1），排在互动内容之后、
    零互动之间按时间排序；``edgerank_enabled=False`` 时返回时间倒序等价分。
    """
    if enabled is None:
        enabled = settings.edgerank_enabled
    if not enabled:
        return 1e18 - _age_seconds(pub_time, now)

    weighted = sum(
        profile.weight(k) * int(counts.get(k, 0)) for k in profile.weights
    )
    d = decay(_age_seconds(pub_time, now), profile.half_life_seconds)
    score = weighted * d if weighted > 0 else _ZERO_INTERACTION_BASE * d

    extra = extra or {}
    score += settings.edgerank_engagement_weight * float(extra.get("engagement", 0.0))
    score += settings.edgerank_rich_weight * (1.0 if extra.get("rich") else 0.0)
    score += settings.edgerank_forward_penalty * (1.0 if extra.get("is_forward") else 0.0)
    score += settings.edgerank_fresh_weight / (1.0 + float(extra.get("exposure", 0)))
    score += settings.edgerank_author_quality_weight * float(extra.get("author_quality", 0.0))
    recent = int(extra.get("recent_publish", 0))
    threshold = settings.edgerank_author_publish_threshold
    if recent > threshold:
        score -= settings.edgerank_author_spam_penalty * (recent - threshold)
    score -= settings.edgerank_dislike_penalty * float(extra.get("dislike_ratio", 0.0))
    # 2.37.0：举报数降权（pending 举报，按 resourceType+bizId 统计，全资源通用）
    score -= settings.edgerank_report_penalty * float(extra.get("report_count", 0))
    # 2.37.0：作者粉丝/等级（log 压缩粉丝量 + 线性等级加权）
    score += settings.edgerank_fans_weight * log(1 + int(extra.get("fans", 0)))
    score += settings.edgerank_level_weight * float(extra.get("level", 0))
    return score


def compute_topic_score(
    topic: Any,
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
        return 1e18 - _age_seconds(getattr(topic, "pubTime", None), now)

    total = (
        profile.weight(TOPIC_DYN_COUNT) * log(int(topic.dynCount or 0) + 1)
        + profile.weight(TOPIC_VIEW_COUNT) * log(int(topic.viewCount or 0) + 1)
        + profile.weight(TOPIC_IS_HOT) * int(topic.isHot or 0)
        + profile.weight(TOPIC_SORT_WEIGHT) * int(topic.sortWeight or 0)
    )
    return total * decay(_age_seconds(topic.pubTime, now), profile.half_life_seconds)


# ==================== 三套 Profile（权重独立，可配置） ====================

FEED_PROFILE = EdgeRankProfile(
    weights=settings.edgerank_feed_weights,
    half_life_seconds=float(settings.edgerank_feed_half_life_seconds),
)

TOPIC_FEED_PROFILE = EdgeRankProfile(
    weights=settings.edgerank_topic_feed_weights,
    half_life_seconds=float(settings.edgerank_topic_feed_half_life_seconds),
)

TOPIC_SQUARE_PROFILE = EdgeRankProfile(
    weights=settings.edgerank_topic_square_weights,
    half_life_seconds=float(settings.edgerank_topic_square_half_life_seconds),
)


def build_anon_profile(
    seed: str | int | None,
    *,
    ratio: float | None = None,
) -> EdgeRankProfile:
    """基于 ``FEED_PROFILE`` 随机扰动生成匿名推荐 Profile（2.34.0）。

    未登录用户不以全局排序返回：以 ``seed``（如客户端 ``uniq_id``）为随机种子，
    对每项权重乘 ``[1-ratio, 1+ratio]`` 的确定性扰动（``random.Random(seed)``），
    使不同匿名用户/会话在分数相近内容间看到不同顺序（整体仍以热度为基调）。
    ``seed`` 为空时每次调用随机；``ratio`` 缺省取
    ``settings.edgerank_anon_perturb_ratio``。
    """
    r = settings.edgerank_anon_perturb_ratio if ratio is None else ratio
    rng = random.Random(seed)
    weights = {
        k: v * (1 + rng.uniform(-r, r))
        for k, v in FEED_PROFILE.weights.items()
    }
    return EdgeRankProfile(
        weights=weights,
        half_life_seconds=FEED_PROFILE.half_life_seconds,
    )

__all__ = [
    "COMMENT",
    "FAVORITE",
    "FEED_PROFILE",
    "LIKE",
    "REPOST",
    "SHARE",
    "TOPIC_FEED_PROFILE",
    "TOPIC_SQUARE_PROFILE",
    "VIEW",
    "EdgeRankProfile",
    "build_anon_profile",
    "build_moment_counts",
    "compute_moment_score",
    "compute_topic_score",
    "decay",
]
