"""ALS 协同召回（2.66.0，机器学习推荐 P1 召回）。

**隐语义协同过滤**：把「用户 × 资源」的隐式反馈矩阵（点赞/收藏/评论/转发）分解成
低维隐向量，用向量内积估计用户对未见过资源的兴趣——这是传统机器学习里做召回的
主力方法（矩阵分解 / ALS），与「内容向量（语义）召回」互补：

- 语义召回靠**内容相似**（文本 embedding）→ 解决冷启动；
- ALS 靠**行为共现**（谁和谁一起被同一批人互动）→ 解决「和你口味相近的人在看什么」。

训练：``scripts/train_als_recall.py`` 产出 ``npz``（user_ids / item_ids / user_factors /
item_factors）。在线只做「查用户向量 → 与全部物品向量点积 → top-N」，是纯内存运算。

**冷启动**：用户不在训练矩阵里（新用户 / 无互动）→ 本路跳过（返回空），
由热门 / 语义 / 兜底路负责，正是多路召回的意义。
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from datetime import datetime

import numpy as np
from loguru import logger

from app.core.config import settings
from app.models.schemas.ml_status import MLModelStatus
from app.models.schemas.sys_config import FeedMLConfig


@dataclass(frozen=True, slots=True)
class ALSIndex:
    """ALS 模型索引（不可变，加载后只读）。"""

    user_ids: np.ndarray  # 行号 → mid
    item_ids: np.ndarray  # 列号 → bizId
    user_factors: np.ndarray  # (n_users, k)
    item_factors: np.ndarray  # (n_items, k)

    def recall(self, viewer_mid: int, limit: int) -> list[int]:
        """取该用户最感兴趣的 N 个资源 bizId（用户不在索引中返回空）。"""
        if limit <= 0:
            return []
        pos = int(np.searchsorted(self.user_ids, viewer_mid))
        if pos >= len(self.user_ids) or int(self.user_ids[pos]) != viewer_mid:
            return []
        user_vec = self.user_factors[pos]
        scores = self.item_factors @ user_vec
        k = min(limit, len(self.item_ids))
        top = np.argpartition(-scores, k - 1)[:k]
        top = top[np.argsort(-scores[top])]
        return [int(self.item_ids[i]) for i in top]


class ALSRecallService:
    """ALS 索引加载与召回（进程内单例，按 mtime 热重载）。"""

    def __init__(self) -> None:
        self._index: ALSIndex | None = None
        self._mtime: float = 0.0
        self._path: str = ""
        self._checked_at: float = 0.0

    def _maybe_reload(self) -> None:
        now = time.monotonic()
        if self._index is not None and now - self._checked_at < max(
            settings.ml_ranker_reload_seconds, 1
        ):
            return
        self._checked_at = now
        path = settings.ml_als_model_path
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            if self._index is not None:
                logger.warning(f"[als_recall] 模型文件消失：{path}，本路跳过")
            self._index = None
            return
        if self._index is not None and mtime == self._mtime and path == self._path:
            return
        try:
            with np.load(path) as data:
                index = ALSIndex(
                    user_ids=data["user_ids"],
                    item_ids=data["item_ids"],
                    user_factors=data["user_factors"],
                    item_factors=data["item_factors"],
                )
            self._index, self._path, self._mtime = index, path, mtime
            logger.info(
                f"[als_recall] 已加载索引：{path}"
                f"（users={len(index.user_ids)}, items={len(index.item_ids)}）"
            )
        except Exception as e:  # pragma: no cover - 模型损坏时降级
            logger.warning(f"[als_recall] 加载索引失败，本路跳过: {e}")
            self._index = None

    def status(self, path_str: str) -> MLModelStatus:
        """索引状态（在线是否已加载 + 规模），供 ``/feed/ml-status`` 展示。"""
        try:
            st = os.stat(path_str)
            mtime: datetime | None = datetime.fromtimestamp(st.st_mtime)
            size = int(st.st_size)
        except OSError:
            mtime, size = None, 0
        loaded = self._index is not None
        return MLModelStatus(
            ready=loaded,
            path=path_str,
            sizeBytes=size,
            trainedAt=mtime,
            metrics=(
                {"users": len(self._index.user_ids), "items": len(self._index.item_ids)}
                if loaded and self._index is not None
                else None
            ),
        )

    def recall(
        self,
        viewer_mid: int | None,
        limit: int | None = None,
        config: FeedMLConfig | None = None,
    ) -> list[int]:
        """召回该用户可能感兴趣的资源（开关关闭 / 未登录 / 冷启动 → 空列表）。

        ``config`` 为运行时配置（``feed_ml`` 配置项，2.66.0）；None 时回落 settings。
        """
        cfg = config or FeedMLConfig(
            als_enabled=settings.ml_als_enabled, als_limit=settings.ml_als_limit
        )
        if not cfg.als_enabled or viewer_mid is None:
            return []
        self._maybe_reload()
        if self._index is None:
            return []
        return self._index.recall(int(viewer_mid), limit or cfg.als_limit)


_service = ALSRecallService()

#: 供状态查询复用（避免重复单例）
als_recall_service = _service


def recall_by_als(
    viewer_mid: int | None,
    limit: int | None = None,
    config: FeedMLConfig | None = None,
) -> list[int]:
    """ALS 召回路入口（弱依赖：任何异常都退化为不召回）。"""
    try:
        return _service.recall(viewer_mid, limit, config)
    except Exception as e:  # pragma: no cover - 召回是弱依赖
        logger.warning(f"[als_recall] 召回失败，本路跳过: {e}")
        return []


__all__ = ["ALSIndex", "ALSRecallService", "als_recall_service", "recall_by_als"]
