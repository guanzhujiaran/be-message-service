"""批量装配用的通用值对象（`Indexed[V]` / `ResourceDisplay`）。

**为什么要有这个模块**：动态 Feed、时间线、审核列表这类「批量装配」场景，
需要一次性把 `id → 值` 都取回来再逐条回填。此前一律用裸 `dict` 承载，问题是：

- 缺键语义含糊（`map.get(k)` 分不清「没查」与「查了没有」）；
- 传参处只能写 `dict[int, X] | None`，无法表达「这是一次批量装配的结果」；
- 项目约定禁止用 dict 承载业务类型。

`Indexed[V]` 是不可变的 `int → V` 聚合：`value_of()` 取不到返回 None（语义明确），
`has()` 区分「本批覆盖但值为 None」这类场景，`items()` / `values()` 供装配遍历。

`ResourceDisplay` 是非 dynamic 资源（lottery 等）的展示信息；
**外部 RPC 返回的 dict 只允许在 `from_rpc` 处转换一次**。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Generic, TypeVar

#: 泛型参数用 TypeVar 而非 PEP 695 语法：本项目 ruff 的 target-version 由
#: `requires-python = ">=3.11"` 推断，PEP 695 会被判为语法错误（运行时虽为 3.13）。
K = TypeVar("K")
V = TypeVar("V")


@dataclass(frozen=True, slots=True)
class Keyed(Generic[K, V]):
    """不可变的 `键 → 值` 聚合（替代裸 dict 的批量装配结果）。

    键可取任意可哈希值：业务 id（int）、`(bizType, bizId)` 复合键（tuple）、
    词级别（enum）或字符串键 —— 都走同一个值对象，不再各造一个映射类。
    """

    slots: tuple[tuple[K, V], ...] = ()

    @classmethod
    def of(cls, rows: Iterable[tuple[K, V]]) -> Keyed[K, V]:
        """由 `(键, 值)` 序列构造（同键后者覆盖前者，与批量回查「后写优先」一致）。"""
        merged: list[tuple[K, V]] = []
        for key, value in rows:
            replaced = False
            for idx, (exist_key, _) in enumerate(merged):
                if exist_key == key:
                    merged[idx] = (key, value)
                    replaced = True
                    break
            if not replaced:
                merged.append((key, value))
        return cls(slots=tuple(merged))

    @classmethod
    def empty(cls) -> Keyed[K, V]:
        """空集合（弱依赖失败 / 无键命中时的降级返回值）。"""
        return cls()

    def value_of(self, key: K) -> V | None:
        """取某键的值（本批未覆盖返回 None，与「值为 None」区分开）。"""
        for slot_key, value in self.slots:
            if slot_key == key:
                return value
        return None

    def value_or(self, key: K, default: V) -> V:
        """取某 id 的值，缺失时用默认值（如计数 0 / 布尔 False）。"""
        value = self.value_of(key)
        return default if value is None else value

    def has(self, key: K) -> bool:
        """本批是否覆盖了该键（与「值为 None」区分开）。"""
        return any(slot_key == key for slot_key, _ in self.slots)

    def merge(self, other: Keyed[K, V]) -> Keyed[K, V]:
        """合并另一批（`other` 覆盖同键项），用于「本批缺失再补查」的追加场景。"""
        return type(self).of((*self.slots, *other.slots))

    def items(self) -> tuple[tuple[int, V], ...]:
        return self.slots

    def keys(self) -> tuple[K, ...]:
        return tuple(key for key, _ in self.slots)

    def values(self) -> tuple[V, ...]:
        return tuple(value for _, value in self.slots)

    def __contains__(self, key: object) -> bool:
        return any(slot_key == key for slot_key, _ in self.slots)

    def __iter__(self):
        return iter(self.slots)

    def __len__(self) -> int:
        return len(self.slots)

    def __bool__(self) -> bool:
        return bool(self.slots)


class Indexed(Keyed[int, V], Generic[V]):
    """`id → 值` 聚合（`Keyed[int, V]` 的语义化别名，业务 id 场景统一用它）。

    `__slots__ = ()`：基类已用 slots，子类不声明的话实例会重新长出 `__dict__`
    （每实例多一份字典，白白吃内存）。
    """

    __slots__ = ()


@dataclass(frozen=True, slots=True)
class ResourceDisplay:
    """非 dynamic 资源的展示信息（标题 / 封面 / 跳转链接，缺失字段为 None）。"""

    name: str | None = None
    cover: str | None = None
    jump_url: str | None = None

    @classmethod
    def from_rpc(cls, raw: Mapping[str, str | None]) -> ResourceDisplay:
        """RPC 返回的 `{name, cover, jumpUrl}` 映射（**唯一的 dict→对象转换点**）。"""
        return cls(
            name=raw.get("name"),
            cover=raw.get("cover"),
            jump_url=raw.get("jumpUrl"),
        )


@dataclass(frozen=True, slots=True)
class OthersLotDynDisplay:
    """第三方抽奖动态的展示信息（标题 / 封面 / 跳转 / 作者，缺失字段为 None）。"""

    name: str | None = None
    cover: str | None = None
    jump_url: str | None = None
    author_mid: int | None = None
    author_name: str | None = None


__all__ = ["Indexed", "Keyed", "OthersLotDynDisplay", "ResourceDisplay"]
