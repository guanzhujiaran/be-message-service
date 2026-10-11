"""机器学习精排（2.66.0）：LightGBM LambdaRank 打分 + 与 EdgeRank 融合。

传统机器学习做推荐排序的最优解（自动特征交叉、支持 listwise 排序目标、推理快、
可解释）：离线用 ``scripts/train_feed_ranker.py`` 训练 LambdaRank，产出模型文件；
在线这里负责：

1. **加载**：按 ``settings.ml_ranker_model_path`` 读模型，按 mtime 热重载（无需重启）；
2. **构造特征**：与离线 ``FEATURE_NAMES`` **同序**（契约见 ``scripts/build_feed_samples.py``）；
3. **打分与融合**：`final = (1−w)·base + w·ml_scaled`，其中 ``ml_scaled`` 把模型分
   按两列均值对齐到 EdgeRank 分的量纲——**避免硬编码量纲**（换模型/换权重不用重调）。

**弱依赖**：模型缺失 / 未开关 / 打分异常 → 一律返回原 EdgeRank 分（Feed 不受影响）。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime
from math import log1p
from pathlib import Path

from loguru import logger

from app.core.config import settings
from app.models.schemas.sys_config import FeedMLConfig


@dataclass(frozen=True, slots=True)
class RankContext:
    """打分上下文（一次请求内不变的部分）。"""

    now: datetime
    hour_of_day: int


@dataclass(frozen=True, slots=True)
class CandidateFeatures:
    """单个候选的可打分特征（由调用方从 ORM 行 / 批量查询结果装配）。"""

    biz_id: int
    mid: int
    pub_time: datetime | None
    topic_count: int
    is_rich: bool
    is_forward: bool
    text_len: int
    like_count: int
    comment_count: int
    repost_count: int
    view_count: int
    author_avg_engagement: float = 0.0
    author_recent_publish: int = 0
    author_fans: int = 0
    author_level: float = 0.0
    is_followed: bool = False
    topic_hit: int = 0
    position: int = 0


#: 与离线 ``scripts/build_feed_samples.py::FEATURE_NAMES`` 一致（**顺序即契约**）
def to_vector(c: CandidateFeatures, ctx: RankContext) -> list[float]:
    """按 FEATURE_NAMES 顺序展开成模型入参向量。"""
    age_hours = (ctx.now - c.pub_time).total_seconds() / 3600.0 if c.pub_time else 0.0
    positive = c.like_count + c.comment_count + c.repost_count
    return [
        log1p(max(age_hours, 0.0)),
        float(c.topic_count),
        1.0 if c.is_rich else 0.0,
        1.0 if c.is_forward else 0.0,
        float(c.text_len),
        float(c.like_count),
        float(c.comment_count),
        float(c.repost_count),
        float(c.view_count),
        positive / max(c.view_count, 1),
        float(c.author_avg_engagement),
        float(c.author_recent_publish),
        log1p(float(c.author_fans)),
        float(c.author_level),
        1.0 if c.is_followed else 0.0,
        float(c.topic_hit),
        float(ctx.hour_of_day),
        float(c.position),
    ]


class FeedMLRanker:
    """LightGBM 精排打分器（进程内单例，模型按 mtime 热重载）。"""

    def __init__(self) -> None:
        self._booster = None
        self._path: str = ""
        self._mtime: float = 0.0
        self._checked_at: float = 0.0

    @property
    def available(self) -> bool:
        """模型是否已加载可用（开关关闭 / 文件缺失均为 False）。"""
        return self._booster is not None

    def _maybe_reload(self) -> None:
        """按 ``ml_ranker_reload_seconds`` 周期检查模型文件 mtime 并热重载。"""
        import time

        now = time.monotonic()
        if self._booster is not None and now - self._checked_at < max(
            settings.ml_ranker_reload_seconds, 1
        ):
            return
        self._checked_at = now
        path = settings.ml_ranker_model_path
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            if self._booster is not None:
                logger.warning(f"[ml_ranker] 模型文件消失：{path}，回退 EdgeRank")
            self._booster = None
            return
        if self._booster is not None and mtime == self._mtime and path == self._path:
            return
        try:
            import lightgbm as lgb

            self._booster = lgb.Booster(model_file=path)
            self._path, self._mtime = path, mtime
            logger.info(f"[ml_ranker] 已加载模型：{path}")
        except Exception as e:  # pragma: no cover - 模型损坏时降级
            logger.warning(f"[ml_ranker] 加载模型失败，回退 EdgeRank: {e}")
            self._booster = None

    def predict(self, vectors: list[list[float]]) -> list[float] | None:
        """批量打分（模型不可用返回 None，调用方保持原分）。"""
        if not settings.ml_ranker_enabled:
            return None
        self._maybe_reload()
        if self._booster is None or not vectors:
            return None
        try:
            raw = self._booster.predict(vectors)
            return [float(x) for x in raw]
        except Exception as e:  # pragma: no cover - 打分异常时降级
            logger.warning(f"[ml_ranker] 打分失败，回退 EdgeRank: {e}")
            return None


_ranker = FeedMLRanker()


def blend_scores(
    base_scores: list[float],
    ml_scores: list[float] | None,
    *,
    weight: float | None = None,
) -> list[float]:
    """把模型分融合进 EdgeRank 分：``final = (1−w)·base + w·ml_scaled``。

    ``ml_scaled`` = ``ml / mean(ml) · mean(base)``——把两列分对齐到同一量纲后再加权，
    这样 ``w`` 是纯粹的「模型话语权」旋钮，不需要为不同模型重新标定系数。
    ``w<=0`` / 模型不可用 / 均值为 0 → 原样返回 base。
    """
    if not ml_scores or len(ml_scores) != len(base_scores):
        return base_scores
    w = settings.ml_ranker_blend_weight if weight is None else weight
    if w <= 0:
        return base_scores
    n = len(base_scores)
    mean_base = sum(base_scores) / n
    mean_ml = sum(ml_scores) / n
    if mean_ml == 0:
        return base_scores
    scale = mean_base / mean_ml
    return [
        (1.0 - w) * b + w * (m * scale)
        for b, m in zip(base_scores, ml_scores, strict=False)
    ]


def score_candidates(
    features: list[CandidateFeatures],
    base_scores: list[float],
    ctx: RankContext,
    config: FeedMLConfig | None = None,
) -> list[float]:
    """在线精排融合入口：``EdgeRank 分 + LightGBM 分`` → 最终分。

    ``config`` 为运行时配置（``feed_ml`` 配置项，2.66.0）；None 时回落 settings 默认值。
    """
    cfg = config or FeedMLConfig(
        ranker_enabled=settings.ml_ranker_enabled,
        ranker_blend_weight=settings.ml_ranker_blend_weight,
    )
    if not cfg.ranker_enabled or len(features) != len(base_scores):
        return base_scores
    vectors = [to_vector(c, ctx) for c in features]
    ml_scores = _ranker.predict(vectors)
    return blend_scores(base_scores, ml_scores, weight=cfg.ranker_blend_weight)


def model_path() -> Path:
    """当前配置的模型文件路径（供训练脚本与运维对齐）。"""
    return Path(settings.ml_ranker_model_path)


__all__ = [
    "CandidateFeatures",
    "FeedMLRanker",
    "RankContext",
    "blend_scores",
    "model_path",
    "score_candidates",
    "to_vector",
]
