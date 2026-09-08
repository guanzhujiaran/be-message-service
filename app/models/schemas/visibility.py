"""字段级可见性：标记 + 序列化期上下文裁剪（计划书 §5.12）。

取代此前的「Public 基类 + Private 子类 + ``to_public()`` 手动投影」：

- 不再用**继承**表达可见性，公开 / 私密字段同处一个 ``XxxOut`` 模型；
- 敏感字段用 ``Annotated[T, Private()]`` **就地标记**，加字段时顺手打标即可；
- 裁剪发生在**序列化期**，由模型自己完成，**嵌套模型自动生效**，外层与调用点零负担；
- 取不到访问者上下文时按「他人视角」裁剪（安全默认）。

两种标记：

- ``Private()`` —— 本人 + 管理员可见（如邮箱、经验、大会员到期）；
- ``Private(admin_only=True)`` —— 仅管理员可见（如评论原始 IP，见决策 C3）。
"""

from __future__ import annotations

from functools import lru_cache
from typing import Annotated, Any, ClassVar

from pydantic import SerializationInfo, model_serializer

from app.core.viewer_context import ViewerContext, get_viewer

__all__ = ["Private", "VisibilityMixin", "private_fields"]


class Private:
    """字段级可见性标记。

    Args:
        admin_only: ``True`` 时该字段**仅管理员**可见，对象归属者本人也不可见。
    """

    __slots__ = ("admin_only",)

    def __init__(self, *, admin_only: bool = False) -> None:
        self.admin_only = admin_only

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Private) and other.admin_only == self.admin_only

    def __hash__(self) -> int:
        return hash((self.__class__, self.admin_only))

    def __repr__(self) -> str:
        return f"Private(admin_only={self.admin_only})"


# 常用别名，减少重复书写
PrivateStr = Annotated[str | None, Private()]
PrivateInt = Annotated[int | None, Private()]
AdminOnlyStr = Annotated[str | None, Private(admin_only=True)]


@lru_cache(maxsize=256)
def private_fields(cls: type) -> tuple[tuple[str, Private], ...]:
    """取某模型上所有打了 :class:`Private` 标记的字段（含标记参数）。"""
    fields = getattr(cls, "model_fields", None) or {}
    found: list[tuple[str, Private]] = []
    for name, field in fields.items():
        for meta in field.metadata or ():
            if isinstance(meta, Private):
                found.append((name, meta))
                break
    return tuple(found)


class VisibilityMixin:
    """给响应模型注入「按访问者裁剪私域字段」的序列化器。

    用法::

        class UserBriefOut(SQLModel, AutoStrMixin, VisibilityMixin):
            mid: int
            email: Annotated[str | None, Private()] = None

    ``_owner_mid_field`` 指定「这个对象属于谁」的字段名，用于判断本人视角；
    没有归属概念的模型（如系统级统计）可设为 ``""``，此时私域字段仅管理员可见。
    """

    _owner_mid_field: ClassVar[str] = "mid"

    @model_serializer(mode="wrap")
    def _serialize_by_visibility(
        self, handler: Any, info: SerializationInfo
    ) -> dict[str, Any]:
        data = handler(self)
        marked = private_fields(type(self))
        if not marked:
            return data

        ctx = info.context or {}
        viewer: ViewerContext = ctx.get("viewer") or get_viewer()
        owner_mid = (
            getattr(self, self._owner_mid_field, None) if self._owner_mid_field else None
        )

        for name, marker in marked:
            if viewer.can_see(owner_mid, admin_only=marker.admin_only):
                continue
            data.pop(name, None)
            # AutoStrMixin 会为 ID 字段生成 ``*Str`` 镜像，一并剥离避免旁路泄漏
            data.pop(f"{name}Str", None)
        return data
