"""多审核环节编排层（阶段一补充，纯新增）。

模型（见 `docs/be-message-审核状态机OOP重构方案.md` §9）：

- **主状态**：资源主表 `auditStatus`（当前可见性），**唯一**决定资源能不能公开 / 互动。
- **多环节**：一个资源经历多个顺序 / 并列审核环节（自动文本 / 人工内容 / 举报处置…），
  每环节是独立 :class:`AuditStateMachine`，各有自己的 `TRANSITIONS` / 规则 / 副作用。
- **编排**：:class:`AuditFlowOrchestrator` 调度各环节流转，并**单一入口**地联动主状态，
  避免多个环节各改各的 `auditStatus` 互相打架。

环节状态与主状态分离：
- 环节自身流转的状态落在"环节记录"（如 `TResourceAuditLog` / 子表 / 环节对象），
  经该环节 `AuditStateMachine.transition()` 校验 + 副作用 + 流水；
- 主 `auditStatus` 由编排器在环节结果后按 `when_primary` 联动写入。

本文件纯新增、自包含，不依赖具体业务表；供接入具体资源时复用。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Mapping

from app.services.moderation.state_machine import (
    AuditStateMachine,
    ModerationAction,
    ResourceAuditStatusEnum,
)

__all__ = [
    "AuditFlow",
    "PrimaryProvider",
    "AuditFlowOrchestrator",
]


@dataclass(frozen=True)
class AuditFlow:
    """描述一个审核环节。

    - `name`：环节名（如 ``"text_auto"`` / ``"content_manual"`` / ``"report"``）；
    - `machine`：本环节自己的状态机（独立 `TRANSITIONS` / 副作用）；
    - `state_attr`：环节状态列名（落在环节记录 / 环节对象上，非主表 `auditStatus`）；
    - `affects_primary`：本环节结果是否联动主 `auditStatus`；
    - `when_primary`：环节结果（语义态）-> 主状态值 的映射（仅 `affects_primary=True` 时用）。
      也可给推导函数（见 `PrimaryProvider`）做更复杂联动。
    """

    name: str
    machine: type[AuditStateMachine]
    state_attr: str
    affects_primary: bool = False
    when_primary: Mapping[ResourceAuditStatusEnum, ResourceAuditStatusEnum] | None = None


class PrimaryProvider(ABC):
    """主状态推导策略：从多个环节当前状态反推资源主 `auditStatus`。

    若资源的环节组合需要"某环节 REJECTED -> 主 REJECTED、全部 NORMAL 才 NORMAL"这类
    跨环节规则，可实现本接口并注入编排器；否则可用简单的 `when_primary` 联动。
    """

    @abstractmethod
    async def derive(self, flow_states: dict[str, ResourceAuditStatusEnum]) -> ResourceAuditStatusEnum:
        """从 ``{flow_name: 环节当前态}`` 推导主状态。"""


class _PrimaryRow:
    """编排器写主状态的最小载体（阶段一用简单对象承载，接入业务时换成真实资源行）。"""

    def __init__(self, audit_status: ResourceAuditStatusEnum):
        self.auditStatus: ResourceAuditStatusEnum = audit_status

    def __repr__(self) -> str:  # pragma: no cover - 仅便于调试
        return f"_PrimaryRow(auditStatus={self.auditStatus.name})"


class AuditFlowOrchestrator:
    """多审核环节编排器：调度各环节流转 + 单一入口联动主状态。

    - 主对象：实现``auditStatus``可读写（接入业务时传资源行，如 `TMoment`）；
    - 环节记录：每环节一个能读写 ``state_attr`` 的对象，作为该环节的当前状态载体。

    用法（接入某个资源时）::

        orch = AuditFlowOrchestrator(
            primary=resource_row,            # 资源主对象（写 auditStatus）
            flows=[
                AuditFlow("content_manual", ContentManualMachine, "auditStatus",
                          affects_primary=True,
                          when_primary={REJECTED: REJECTED, NORMAL: NORMAL}),
            ],
        )
        await orch.run_flow(session, flow_row, "content_manual",
                            action=ModerationAction.APPROVE, actor_mid=admin)
    """

    def __init__(
        self,
        *,
        primary: Any,
        flows: list[AuditFlow],
        provider: PrimaryProvider | None = None,
        primary_attr: str = "auditStatus",
    ) -> None:
        self.primary = primary
        self._flows: dict[str, AuditFlow] = {f.name: f for f in flows}
        self.provider = provider
        self.primary_attr = primary_attr

    def flow(self, name: str) -> AuditFlow:
        """取某环节描述；不存在抛 `KeyError`。"""
        try:
            return self._flows[name]
        except KeyError:
            raise KeyError(f"未知审核环节: {name}") from None

    def has_flow(self, name: str) -> bool:
        return name in self._flows

    async def run_flow(
        self,
        session: Any,
        flow_row: Any,
        flow_name: str,
        *,
        action: ModerationAction,
        actor_mid: int,
        reason: str | None = None,
        remark: str | None = None,
    ) -> ResourceAuditStatusEnum:
        """执行某环节的一次状态迁移。

        1. 用该环节自己的 `machine.transition()` 做环节内流转（合法性校验 / 副作用 / 流水），
           环节状态写到 `flow_row` 的 `state_attr` 列；
        2. 若该环节 ``affects_primary``，则按 ``when_primary``（或 provider）联动主
           `auditStatus` —— 主状态的唯一写入入口在此，防止多环节各改各的。

        Returns:
            该环节迁移后的语义态。
        """
        flow = self.flow(flow_name)
        machine = flow.machine()
        # 环节机写环节记录：临时把 state_attr 指到环节自身的状态列
        machine.state_attr = flow.state_attr
        to = await machine.transition(
            session,
            flow_row,
            action=action,
            actor_mid=actor_mid,
            reason=reason,
            remark=remark,
        )
        if flow.affects_primary:
            await self._apply_primary(flow, to)
        return to

    async def _apply_primary(
        self,
        flow: AuditFlow,
        flow_to: ResourceAuditStatusEnum,
    ) -> None:
        """按环节结果联动主状态（单一入口）。"""
        primary: ResourceAuditStatusEnum | None = None
        if flow.when_primary is not None:
            primary = flow.when_primary.get(flow_to)
        if primary is not None:
            setattr(self.primary, self.primary_attr, primary)

    async def derive_primary(self, flow_states: dict[str, ResourceAuditStatusEnum]) -> ResourceAuditStatusEnum:
        """（可选）用主状态推导策略反推主状态（供复杂跨环节规则）。"""
        if self.provider is None:
            raise RuntimeError("未注入 PrimaryProvider，无法推导主状态")
        return await self.provider.derive(flow_states)
