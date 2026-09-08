"""统一审核/处置状态机（阶段一骨架）。

设计要点（详见 `docs/be-message-审核状态机OOP重构方案.md`）：

1. **一套语义状态**：`ResourceAuditStatusEnum`（`NORMAL=1` 基准），"待审"统一用
   `AUDITING`（方案甲：不单设 `PENDING` 成员，单据的"待审"语义靠 `biz_type`
   区分，避免一套列里两个成员同值混淆）。
2. **显式迁移表**：`AuditStateMachine.TRANSITIONS` 声明"from --action--> to"
   的合法流转；非法流转在 `transition()` 唯一处抛 `StateTransitionError`，
   杜绝在业务方法里散落手写判断。
3. **副作用收敛**：进入某状态后的副作用（Feed 同步 / FORWARD 源计数 /
   通知 / 写公开头像等）集中在 `on_enter` 钩子（子类覆盖，默认空），差异显式化、
   可单测。
4. **唯一写入入口**：所有改审核态的地方统一走 `transition()`，写状态列 +
   触发副作用 + 写审计流水，不做散落赋值。

本文件阶段一为**纯新增、自包含**：不 import 业务表 / 现存量枚举，供阶段二起的
资源接入时使用；`_write_audit_log` 默认空、可由子类覆盖为写 `TResourceAuditLog`。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import IntEnum
from typing import Any

from app.models.enums import ResourceAuditStatusEnum  # noqa: F401  # re-export 便于使用方从本模块导入

__all__ = [
    "ResourceAuditStatusEnum",
    "ModerationAction",
    "Transition",
    "StateTransitionError",
    "AuditStateMachine",
]


class ModerationAction(IntEnum):
    """审核 / 处置动作（流转的动词）。"""

    APPROVE = 1  # 审核通过 / 单据通过
    REJECT = 2  # 审核驳回 / 单据驳回
    HIDE = 3  # 下架（实体类）
    # 评审结论：无申诉恢复场景，不引入 RESTORE；后续需要再加对应 TRANSITIONS 即可。


@dataclass(frozen=True)
class Transition:
    """一条合法迁移：`from_state --(action)--> to_state`。"""

    from_state: ResourceAuditStatusEnum
    action: ModerationAction
    to_state: ResourceAuditStatusEnum


class StateTransitionError(ValueError):
    """非法的状态迁移：当前状态无法执行该动作。

    携带语义化信息便于日志 / 对外展示。
    """

    def __init__(
        self,
        from_state: ResourceAuditStatusEnum | None,
        action: ModerationAction,
    ) -> None:
        self.from_state = from_state
        self.action = action
        super().__init__(
            f"非法状态迁移：from={from_state.name if from_state else 'None'} "
            f"action={action.name}"
        )


class AuditStateMachine(ABC):
    """给"某个资源 / 单据对象"做状态迁移的统一入口（模板方法）。

    子类（每类审核对象一个）只声明三件事，即自动获得完整能力：

    1. `TRANSITIONS`：显式迁移表（合法流转的唯一来源）；
    2. `to_native(state)` / `native_state_of(row)`：语义态 <-> 本对象列值映射
       （阶段二起，对尚未迁移到统一枚举的存量列做映射；默认恒等）；
    3. `on_enter` 钩子：进入某状态的副作用（默认空）。

    其余（读当前态、查规则、写状态列、写审计流水、返回结果）全部收敛在本模板，
    禁止在业务方法里散落手写状态赋值与副作用。
    """

    #: 合法迁移表（from_state, action) -> to_state；子类必须声明
    TRANSITIONS: tuple[Transition, ...] = ()

    #: 该对象主表"审核态"属性名（阶段一后统一为 `auditStatus`）
    state_attr: str = "auditStatus"

    # ---- 需子类实现（可复用默认）----

    def to_native(self, state: ResourceAuditStatusEnum) -> Any:
        """语义态 -> 本对象列值（阶段二起对存量枚举做映射；默认恒等用本枚举）。"""
        return state

    @abstractmethod
    def native_state_of(self, row: Any) -> ResourceAuditStatusEnum:
        """读 row 当前审核态 -> 语义态。"""

    async def on_enter(
        self,
        session: Any,
        row: Any,
        new_state: ResourceAuditStatusEnum,
        *,
        action: ModerationAction,
        actor_mid: int,
        reason: str | None,
        remark: str | None,
    ) -> None:
        """进入 `new_state` 后的副作用（默认空）。

        差异显式化、集中在此，可单测。子类实现 Feed 同步 / FORWARD 源计数 ±1 /
        通知 / 写公开头像 / RPC 等。
        """

    # ---- 模板方法（固定，子类不改）----

    def _find_rule(
        self,
        cur: ResourceAuditStatusEnum,
        action: ModerationAction,
    ) -> Transition | None:
        for t in self.TRANSITIONS:
            if t.from_state is cur and t.action is action:
                return t
        return None

    async def transition(
        self,
        session: Any,
        row: Any,
        *,
        action: ModerationAction,
        actor_mid: int,
        reason: str | None = None,
        remark: str | None = None,
    ) -> ResourceAuditStatusEnum:
        """执行一次状态迁移（唯一入口）。

        流程：读当前态 → 查迁移表（非法则抛 `StateTransitionError`）→ 写状态列 →
        `on_enter` 副作用 → 写审计流水。返回目标语义态。
        """
        cur = self.native_state_of(row)
        rule = self._find_rule(cur, action)
        if rule is None:
            raise StateTransitionError(cur, action)

        to = rule.to_state
        setattr(row, self.state_attr, self.to_native(to))
        await self.on_enter(
            session,
            row,
            to,
            action=action,
            actor_mid=actor_mid,
            reason=reason,
            remark=remark,
        )
        await self._write_audit_log(
            session,
            row,
            from_state=cur,
            to_state=to,
            action=action,
            actor_mid=actor_mid,
            reason=reason,
            remark=remark,
        )
        return to

    async def _write_audit_log(
        self,
        session: Any,
        row: Any,
        *,
        from_state: ResourceAuditStatusEnum,
        to_state: ResourceAuditStatusEnum,
        action: ModerationAction,
        actor_mid: int,
        reason: str | None,
        remark: str | None,
    ) -> None:
        """写通用审核流水（`TResourceAuditLog`）。默认空，子类接入具体资源时实现。

        阶段一保持空实现以保证纯新增、不触碰存量表；阶段三推广到评论 / 私信 /
        头像 / 封面时，在此统一写 `TResourceAuditLog`（`bizType+bizId` 定位）。
        """
