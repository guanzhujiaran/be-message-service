"""类型 → 处理器 注册表（单一真相源：handler + 枚举元数据）。

本模块只定义 ``EventSpec`` 规格与**空的** ``EVENT_REGISTRY`` 容器；
具体的「类型 → 处理器」映射由 ``handlers`` 模块在定义完处理器类后填入，
从而避免 ``base ↔ registry ↔ handlers`` 的循环导入：

- ``base`` 直接 ``from .registry import EVENT_REGISTRY``（本模块无任何内部依赖）；
- ``handlers`` 既依赖 ``base.BaseEvent`` 又在本模块填充 ``EVENT_REGISTRY``；
- ``base._resolve_handler_cls`` 读取本注册表，未登记类型回落到 ``GenericEvent``。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from bili_common.models import InteractionActionTypeEnum

if TYPE_CHECKING:
    from .base import BaseEvent


@dataclass(frozen=True)
class EventSpec:
    """单个互动操作类型的完整规格（一个动作 = 一处定义）。

    - ``event_type``：对应的 ``InteractionActionTypeEnum`` 成员（DB 落地值，纯值载体）；
    - ``handler_cls``：该类型的 msgfeed 处理器类（``blocked_silent`` / ``setting_gate``
      等投递语义作为类属性定义在 ``handler_cls`` 上，不污染底层枚举）；
    - ``blocked_silent`` / ``setting_gate``：从 ``handler_cls`` 读取，
      新增动作只需在 handler 类上声明这两个类属性即可生效。

    新增一个互动操作类型时：① 在 bili-common 的 ``InteractionActionTypeEnum``
    加一个纯值成员；② 在 ``handlers`` 模块里加一个处理器类（声明 ``event_type`` /
    ``blocked_silent`` / ``setting_gate``），并加一行
    ``EVENT_REGISTRY[member] = EventSpec(member, HandlerClass)``。
    """

    event_type: InteractionActionTypeEnum
    handler_cls: "type[BaseEvent]"

    @property
    def blocked_silent(self) -> bool:
        # 投递语义已下沉到 handler 类（BaseEvent.blocked_silent），不再读底层枚举
        return self.handler_cls.blocked_silent

    @property
    def setting_gate(self) -> "str | None":
        return self.handler_cls.setting_gate


# 类型 → 处理器 注册表（空容器，由 handlers 模块填充）：
# 未登记的类型由 ``base._resolve_handler_cls`` 回落到 GenericEvent（通用内容体）。
EVENT_REGISTRY: dict[InteractionActionTypeEnum, "EventSpec"] = {}
