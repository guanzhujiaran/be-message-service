"""动态域的通用值对象（替代裸 `dict[str, int]` 之类的统计返回）。

- `ResourceCountPair`：单条动态的「点赞 / 收藏」计数对（点查封装用）；
- `MomentUpStat`：空间动态统计（对标 B 站 `/x/space/upstat`），
  原先由 `get_upstat()` 返回 `dict[str, int]`，调用方只能 `upstat.get("dynamic_count")` 猜键名。

均为不可变（`frozen=True, slots=True`），并负责从 ORM 行转换（**转换点只有一处**）。
"""

from __future__ import annotations

from dataclasses import dataclass

from app.models.db import TInteractionStat


@dataclass(frozen=True, slots=True)
class ResourceCountPair:
    """动态的互动计数对（`TInteractionStat` 中与卡片展示相关的两个计数）。"""

    like_count: int = 0
    favorite_count: int = 0

    @classmethod
    def from_stat(cls, stat: TInteractionStat | None) -> ResourceCountPair:
        """由计数行转换（行不存在时全 0 —— 未互动过的动态没有计数行是正常状态）。"""
        if stat is None:
            return cls()
        return cls(
            like_count=int(stat.likeCount or 0),
            favorite_count=int(stat.favoriteCount or 0),
        )


@dataclass(frozen=True, slots=True)
class MomentUpStat:
    """空间动态统计（对外可见动态数 + 这些动态的获赞总数）。"""

    dynamic_count: int = 0
    like_count: int = 0


__all__ = ["MomentUpStat", "ResourceCountPair"]
