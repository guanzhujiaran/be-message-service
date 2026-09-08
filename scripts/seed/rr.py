"""确定性轮遍游标与分布采样。

seed 全流程用「定值轮遍」取代 ``random.choice / random.sample / random.randint``：
每个用户 / 每条素材都被均匀轮到、不重复，且结果可复现。
"""
from collections.abc import Sequence
from typing import TypeVar

_T = TypeVar("_T")


class _RoundRobin:
    """确定性轮遍游标：从 ``seq`` 轮流取下一项、取完回开头（single-queue 单线程安全）。

    用于取代 ``random.choice / random.sample / random.randint`` 的“随机挑”，
    让 seed 全流程**定值轮遍**（每个用户 / 每条素材都被均匀轮到、不重复）：
    - ``pick(seq)``：取下一项（返回元素类型同 ``seq``，兼容 list/range 等 Sequence）；
    - ``pick_n(seq, n)``：取连续 ``n`` 项（池足够大时不重复，循环后从头再来）。
    取空序列抛 ``ValueError``。
    """

    __slots__ = ("_i",)

    def __init__(self) -> None:
        self._i = 0

    def pick(self, seq: Sequence[_T]) -> _T:
        if not seq:
            raise ValueError("_RoundRobin.pick 从空序列取值")
        v = seq[self._i % len(seq)]
        self._i += 1
        return v

    def pick_n(self, seq: Sequence[_T], n: int) -> list[_T]:
        return [self.pick(seq) for _ in range(max(0, n))]

    def reset(self) -> None:
        self._i = 0


# 全 seed 共享的确定性轮遍游标（单事件循环顺序调用，跨阶段共用一条数据流）
_rr = _RoundRobin()


def _sample(distribution: Sequence[tuple[int, float]]) -> int:
    """按分布**确定性轮遍**取值（distribution = [(值, 权重), ...]，取代随机加权采样）。

    权重可为小数，按 ×2 转整后做累计；每调用一次由全局 ``_rr`` 推进一格，命中对应累计区间，
    使整体分布比例≈权重且可复现（不再随机）。
    """
    scale = 2  # 权重里最多一位小数，×2 转整数
    items: list[tuple[int, int]] = []
    total = 0
    for value, weight in distribution:
        w = int(round(weight * scale))
        total += w
        items.append((value, w))
    bucket = _rr.pick(range(total))
    acc = 0
    for value, w in items:
        acc += w
        if bucket < acc:
            return value
    return items[-1][0]
