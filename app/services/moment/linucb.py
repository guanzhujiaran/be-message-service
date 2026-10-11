"""LinUCB 上下文老虎机（2.66.0，机器学习推荐 P1 探索）。

**为什么从 Thompson Sampling 升级到 LinUCB**：2.65.0 的 TS 只看「这条内容的
互动/曝光计数」，是**无特征**的 bandit——它无法把在 A 内容上学到的经验泛化到
B 内容（新内容永远从先验重新开始）。LinUCB（Li et al. 2010）用**上下文特征**建模
奖励，于是：

- 见过的内容：用自身历史（A/b）估计期望收益 + 置信区间；
- 没见过的内容：**由相似内容的参数泛化出**一个初始估计（冷启动也能合理探索）。

公式（每个资源一个臂）：

    ucb = θᵀx + α · sqrt(xᵀ A⁻¹ x),   θ = A⁻¹ b

- 第一项「利用」：该内容在当前上下文下的预估收益；
- 第二项「探索」：估计的不确定性（数据越少越大 → 自动给新内容机会）。

**更新不在请求路径**：A/b 由 ``scripts/train_linucb.py`` 回放事件离线重算后整行
覆盖到 ``TFeedBanditArm``，在线只读不写（请求路径保持只读，避免事务膨胀与锁竞争）。
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from loguru import logger

from app.core.config import settings


#: 上下文特征维度（与训练脚本一致；改维度需重训，否则旧臂不可用）
def feature_dim() -> int:
    """当前配置的上下文特征维度 d（A 为 d×d、b 为 d 维）。"""
    return max(1, int(settings.ml_linucb_dim))


@dataclass(frozen=True, slots=True)
class ArmContext:
    """单个候选的上下文（内容侧 + 观众侧，全部为可在线计算的量）。"""

    is_rich: bool = False
    is_forward: bool = False
    topic_count: int = 0
    text_len: int = 0
    age_hours: float = 0.0
    engagement: float = 0.0
    author_quality: float = 0.0
    is_followed: bool = False
    topic_hit: int = 0


def context_vector(c: ArmContext) -> np.ndarray:
    """上下文 → d 维特征向量（不足补 0，超出截断；含常数项便于学全局偏置）。"""
    d = feature_dim()
    raw = [
        1.0,  # 常数项
        1.0 if c.is_rich else 0.0,
        1.0 if c.is_forward else 0.0,
        float(c.topic_count),
        float(c.text_len) / 100.0,
        float(c.age_hours) / 24.0,
        float(c.engagement),
        float(c.author_quality),
        1.0 if c.is_followed else 0.0,
        float(c.topic_hit),
    ]
    vec = np.zeros(d, dtype=np.float64)
    for i, v in enumerate(raw[:d]):
        vec[i] = v
    return vec


def ucb_score(
    x: np.ndarray,
    *,
    a_matrix: np.ndarray | None = None,
    b_vector: np.ndarray | None = None,
    alpha: float | None = None,
) -> float:
    """计算单臂 UCB：``θᵀx + α·sqrt(xᵀA⁻¹x)``。

    ``a_matrix`` 为 None（冷启动）→ A = I，θ = 0，退化为「纯探索」：
    ucb = α·‖x‖，即特征越强的内容先被探索。
    """
    d = feature_dim()
    if a_matrix is None or b_vector is None:
        a_matrix = np.eye(d)
        b_vector = np.zeros(d)
    a = np.asarray(a_matrix, dtype=np.float64).reshape(d, d)
    b = np.asarray(b_vector, dtype=np.float64).reshape(d)
    a_alpha = settings.ml_linucb_alpha if alpha is None else alpha
    try:
        a_inv = np.linalg.inv(a)
    except np.linalg.LinAlgError:  # 奇异矩阵（理论上不会，防御）
        a_inv = np.linalg.pinv(a)
    theta = a_inv @ b
    exploit = float(theta @ x)
    explore = float(np.sqrt(max(float(x @ a_inv @ x), 0.0)))
    return exploit + a_alpha * explore


def blend_ucb(
    base_scores: list[float],
    ucb_scores: list[float] | None,
    weight: float | None = None,
) -> list[float]:
    """把 UCB 探索分叠加到精排分上（零均值化后按权重加，避免整体抬升破坏量纲）。

    ``weight`` 来自运行时配置（``feed_ml.linucb_weight``）；None 时回落 settings。
    """
    if not ucb_scores or len(ucb_scores) != len(base_scores):
        return base_scores
    w = settings.ml_linucb_weight if weight is None else weight
    if w <= 0:
        return base_scores
    arr = np.asarray(ucb_scores, dtype=np.float64)
    if arr.std() == 0:
        return base_scores
    # 标准化 → 零均值单位方差，再按权重加到精排分（探索只改相对顺序，不改整体量级）
    norm = (arr - arr.mean()) / (arr.std() + 1e-9)
    base = np.asarray(base_scores, dtype=np.float64)
    scale = float(np.mean(np.abs(base))) + 1e-9
    return [float(b + w * scale * n) for b, n in zip(base, norm, strict=False)]


def load_arms(rows) -> dict[int, tuple[np.ndarray, np.ndarray]]:
    """``TFeedBanditArm`` 行 → ``{bizId: (A, b)}``（只取维度匹配的行）。"""
    d = feature_dim()
    out: dict[int, tuple[np.ndarray, np.ndarray]] = {}
    for row in rows:
        try:
            biz_id = int(row.bizId)
            if int(row.dim) != d:
                continue  # 维度变更后的旧臂忽略（等价于冷启动）
            a = np.asarray(row.aMatrix, dtype=np.float64)
            b = np.asarray(row.bVector, dtype=np.float64)
            if a.size != d * d or b.size != d:
                continue
            out[biz_id] = (a.reshape(d, d), b.reshape(d))
        except (TypeError, ValueError) as e:  # pragma: no cover - 脏数据防御
            logger.warning(
                f"[linucb] 忽略非法臂参数 bizId={getattr(row, 'bizId', '?')}: {e}"
            )
    return out


__all__ = [
    "ArmContext",
    "blend_ucb",
    "context_vector",
    "feature_dim",
    "load_arms",
    "ucb_score",
]
