"""用户展示信息（pptr 主数据的只读快照）批量值对象。

用户主数据（昵称 / 头像 / 等级 / 大会员）只有一份，在 pptr Postgres；be-message 的
关系列表按 mid 批量回查后内联。改造前这份批量结果用裸 `dict[int, UserBriefOut]` 传递，
调用方 `of_mid(mid)` 才能表达「查了但对方已注销」与「本批没覆盖」的区别。

`UserBriefs` 把语义显式化：`has(mid)` = 本批请求里包含该 mid；`of_mid(mid)` =
其展示信息（None 表示已注销 / 回查失败，前端渲染「账号已注销」）。
"""

from __future__ import annotations

from dataclasses import dataclass

from app.models.schemas.user_brief import UserBriefOut

#: 单条用户展示信息（别名，便于调用方表达「一批 brief」的元素类型）
UserBrief = UserBriefOut


@dataclass(frozen=True, slots=True)
class UserBriefSlot:
    """一条用户展示信息（`brief is None` = 已注销 / pptr 回查失败）。"""

    mid: int
    brief: UserBriefOut | None = None


@dataclass(frozen=True, slots=True)
class UserBriefs:
    """一批用户展示信息（不可变，替代裸 dict 映射）。"""

    slots: tuple[UserBriefSlot, ...] = ()

    @classmethod
    def of(cls, rows: list[tuple[int, UserBriefOut | None]]) -> UserBriefs:
        """由 `(mid, brief)` 行构造。"""
        return cls(
            slots=tuple(UserBriefSlot(mid=int(mid), brief=brief) for mid, brief in rows)
        )

    def of_mid(self, mid: int) -> UserBriefOut | None:
        """取某用户的展示信息（未包含或已注销均返回 None）。"""
        for slot in self.slots:
            if slot.mid == int(mid):
                return slot.brief
        return None

    def has(self, mid: int) -> bool:
        """本批数据是否覆盖了该 mid。"""
        return any(slot.mid == int(mid) for slot in self.slots)

    def pairs(self) -> tuple[tuple[int, UserBriefOut | None], ...]:
        """`(mid, brief)` 元组序列（需要按 mid 遍历装配时用，如批量回填）。"""
        return tuple((slot.mid, slot.brief) for slot in self.slots)

    def __iter__(self):
        return iter(self.slots)

    def __len__(self) -> int:
        return len(self.slots)


__all__ = ["UserBrief", "UserBriefSlot", "UserBriefs"]
