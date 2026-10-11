"""关注分组的归属与计数值对象。

改造前这两处返回的是裸 `dict`（`dict[int, int]` / `dict[int, list[int]]`），
调用方只能 `counts.get(tag_id, 0)`、`assigned.get(mid, [])` 地猜语义，
也违反「禁止用 dict 承载业务类型」的项目约定。这里收敛成两个不可变值对象：

- `TagCounts`：某用户各分组的成员数（含系统分组 `-10`）；
- `TagMembership`：某个关注对象所属的分组集合（升序去重）。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class TagCount:
    """单个分组的成员数。"""

    tag_id: int
    count: int


@dataclass(frozen=True, slots=True)
class TagCounts:
    """某用户各分组的成员数（只包含「有成员」的分组，空分组由定义表补齐）。"""

    entries: tuple[TagCount, ...] = ()

    @classmethod
    def of(cls, rows: list[tuple[int, int]]) -> TagCounts:
        """由 `(tag_id, count)` 行构造。"""
        return cls(
            entries=tuple(TagCount(tag_id=int(t), count=int(c or 0)) for t, c in rows)
        )

    def count_of(self, tag_id: int) -> int:
        """某分组的人数（不存在返回 0 —— 空分组是常态，不是错误）。"""
        for entry in self.entries:
            if entry.tag_id == tag_id:
                return entry.count
        return 0

    def __iter__(self):
        return iter(self.entries)

    def __len__(self) -> int:
        return len(self.entries)


@dataclass(frozen=True, slots=True)
class TagMembership:
    """某个关注对象所属的分组集合（按分组 id 升序去重）。"""

    target_mid: int
    tag_ids: tuple[int, ...] = ()

    @classmethod
    def of(cls, target_mid: int, tag_ids: list[int]) -> TagMembership:
        return cls(
            target_mid=int(target_mid), tag_ids=tuple(sorted({int(t) for t in tag_ids}))
        )

    def includes(self, tag_id: int) -> bool:
        return tag_id in self.tag_ids

    def without_system(self) -> tuple[int, ...]:
        """只保留自建分组（`> 0`），系统分组单独处理。"""
        return tuple(t for t in self.tag_ids if t > 0)

    def __bool__(self) -> bool:
        """是否落在任意分组里（False = 属于「默认分组」）。"""
        return bool(self.tag_ids)


__all__ = ["TagCount", "TagCounts", "TagMembership"]
