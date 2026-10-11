"""空间可见性上下文：把「被查看者 + 访问者 + 开关」收敛成一个可判定的对象。

改造前，隐私判断散在四处各自为政：

- `SpacePrivacyService.apply_flags_to_info`（逐字段 if）；
- `FollowService._ledger_visible_to_viewer(..., attribute: AttributeKey)`（按枚举取开关）；
- `FollowService.list_fans` 里内联的 `flags.show_fans_list`；
- `SpaceTimelineService.list` 里内联的 `flags.show_follow_list` / `show_like_list`。

现在统一成 `SpaceVisibility`：一次请求构造一次，之后所有「能不能看 / 要不要裁剪」
都问它，规则只有一处（`UserAttribute.visible`）。
"""

from __future__ import annotations

from dataclasses import dataclass

from app.models.schemas.space import SpaceInfoResp, SpacePrivacyFlags
from app.services.user.space.attributes import (
    AttributeKey,
    AttributeScene,
    attribute_of,
    attributes_of,
)


@dataclass(frozen=True, slots=True)
class SpaceVisibility:
    """一次请求的空间可见性判定上下文（不可变）。

    - `owner_mid`：被查看的空间主人；
    - `viewer_mid`：访问者（未登录为 None）；
    - `flags`：主人已落库的开关快照（`SpacePrivacyService.get_flags`）。
    """

    owner_mid: int
    viewer_mid: int | None
    flags: SpacePrivacyFlags

    @property
    def is_owner(self) -> bool:
        """访问者是否就是空间主人（本人恒可见全部隐私维度）。"""
        return self.viewer_mid is not None and int(self.viewer_mid) == int(
            self.owner_mid
        )

    def enabled(self, key: AttributeKey) -> bool:
        """开关的原始值（不掺视角判断，便于设置页 / 调试直接读）。"""
        return bool(getattr(self.flags, str(key.value), False))

    def can_view(self, key: AttributeKey) -> bool:
        """该维度对当前访问者是否可见（**唯一的可见性判定出口**）。"""
        return attribute_of(key).visible(
            enabled=self.enabled(key), is_owner=self.is_owner
        )

    def can_view_scene(self, scene: AttributeScene) -> tuple[bool, ...]:
        """某场景下各维度的可见性（顺序与注册表一致），供时间线批量判定。"""
        return tuple(self.can_view(attr.key) for attr in attributes_of(scene))

    def mask_profile(self, info: SpaceInfoResp) -> None:
        """按敏感原文类（`PROFILE_FIELD`）的开关裁剪资料字段（原地修改）。"""
        if self.is_owner:
            return
        for attr in attributes_of(AttributeScene.PROFILE_FIELD):
            if not self.can_view(attr.key):
                attr.mask(info)


__all__ = ["SpaceVisibility"]
