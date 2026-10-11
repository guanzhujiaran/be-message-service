"""空间统计数字值对象（固定展示、不设隐私开关）。

改造前，统计数字在路由层被拆成两段内联赋值（`SpaceFollowStat(...)` / `SpaceUpStat(...)`），
来源也是三个不同的服务（关注计数 / 动态统计 / 空间访问计数），调用方要自己知道
「哪个数字来自哪个域」。`SpaceStatSummary` 把同一份「用户空间统计」聚成一个对象，
并负责转成对外 DTO 的两个子结构 —— 加数字只在这里加一处。
"""

from __future__ import annotations

from dataclasses import dataclass

from app.models.schemas.space import SpaceFollowStat, SpaceUpStat


@dataclass(frozen=True, slots=True)
class SpaceStatSummary:
    """空间主人的统计数字汇总（关注 / 粉丝 / 互关 / 动态 / 获赞 / 被访问）。"""

    following_count: int = 0
    follower_count: int = 0
    mutual_count: int = 0
    dynamic_count: int = 0
    like_count: int = 0
    view_count: int = 0

    def with_view_count(self, view_count: int) -> SpaceStatSummary:
        """补上「空间被访问次数」（来源与其他数字不同的独立查询）。"""
        return SpaceStatSummary(
            following_count=self.following_count,
            follower_count=self.follower_count,
            mutual_count=self.mutual_count,
            dynamic_count=self.dynamic_count,
            like_count=self.like_count,
            view_count=int(view_count or 0),
        )

    def to_follow_stat(self) -> SpaceFollowStat:
        """关系统计子结构（对外 DTO，不含 mid —— 外层 SpaceInfoResp 已有）。"""
        return SpaceFollowStat(
            following_count=self.following_count,
            follower_count=self.follower_count,
            mutual_count=self.mutual_count,
        )

    def to_up_stat(self) -> SpaceUpStat:
        """UP 统计子结构（对外 DTO）。"""
        return SpaceUpStat(
            dynamic_count=self.dynamic_count,
            like_count=self.like_count,
        )


__all__ = ["SpaceStatSummary"]
