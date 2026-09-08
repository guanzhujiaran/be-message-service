"""多审核环节编排层单测（阶段一补充，纯单元测试，不依赖数据库）。

覆盖 `app/services/moderation/flow.py`：
- 一资源挂多环节，各自独立流转合法；
- `affects_primary=True` 的环节结果联动主 `auditStatus`；
- `affects_primary=False` 的环节（如自动文本 PASS 不阻断 / 举报单独立）不改主状态；
- 主状态单一入口：多环节联动靠 `when_primary` 决定，不互相覆盖；
- 未知环节抛 `KeyError`；
- 环节非法流转仍抛 `StateTransitionError`。
"""
import pytest

from app.services.moderation.flow import AuditFlow, AuditFlowOrchestrator, _PrimaryRow
from app.services.moderation.state_machine import (
    AuditStateMachine,
    ModerationAction,
    ResourceAuditStatusEnum,
    StateTransitionError,
    Transition,
)


class _FakeSession:
    def __init__(self):
        self.calls: list[str] = []


class _FlowRow:
    """模拟一个"环节记录"对象：环节状态落在自己身上，不是主表行。"""

    def __init__(self, state_attr: str, state: ResourceAuditStatusEnum):
        setattr(self, state_attr, state)


def _make_flow_machine(transitions) -> type[AuditStateMachine]:
    """构造一个最小环节机子类：TRANSITIONS 可配置，状态恒等映射。"""

    class _Machine(AuditStateMachine):
        TRANSITIONS = transitions

        def native_state_of(self, row) -> ResourceAuditStatusEnum:
            return ResourceAuditStatusEnum(int(getattr(row, self.state_attr)))

    return _Machine


# 人工内容审核环节：AUDITING -> NORMAL(通过) / REJECTED(驳回)；REJECTED -> NORMAL(重审)
_CONTENT_TRANSITIONS = (
    Transition(ResourceAuditStatusEnum.AUDITING, ModerationAction.APPROVE, ResourceAuditStatusEnum.NORMAL),
    Transition(ResourceAuditStatusEnum.AUDITING, ModerationAction.REJECT, ResourceAuditStatusEnum.REJECTED),
    Transition(ResourceAuditStatusEnum.REJECTED, ModerationAction.APPROVE, ResourceAuditStatusEnum.NORMAL),
    Transition(ResourceAuditStatusEnum.NORMAL, ModerationAction.REJECT, ResourceAuditStatusEnum.REJECTED),
    Transition(ResourceAuditStatusEnum.NORMAL, ModerationAction.HIDE, ResourceAuditStatusEnum.HIDDEN),
    Transition(ResourceAuditStatusEnum.AUDITING, ModerationAction.HIDE, ResourceAuditStatusEnum.HIDDEN),
)

# 举报处置环节（独立单，不影响资源可见性主状态）：PENDING(用 AUDITING 表达待审) 与资源解耦
_REPORT_TRANSITIONS = (
    Transition(ResourceAuditStatusEnum.AUDITING, ModerationAction.APPROVE, ResourceAuditStatusEnum.NORMAL),
    Transition(ResourceAuditStatusEnum.AUDITING, ModerationAction.REJECT, ResourceAuditStatusEnum.REJECTED),
)

_ContentMachine = _make_flow_machine(_CONTENT_TRANSITIONS)
_ReportMachine = _make_flow_machine(_REPORT_TRANSITIONS)


@pytest.fixture
def session():
    return _FakeSession()


def _resource(primary_state: ResourceAuditStatusEnum):
    return _PrimaryRow(primary_state)


def _make_orchestrator(primary, content_affects=True, report_affects=False):
    """默认编排器：内容环节联动主状态，举报环节不联动（模拟资源的多环节）。"""
    flows = [
        AuditFlow(
            "content_manual", _ContentMachine, "auditStatus",
            affects_primary=content_affects,
            when_primary={
                ResourceAuditStatusEnum.NORMAL: ResourceAuditStatusEnum.NORMAL,
                ResourceAuditStatusEnum.REJECTED: ResourceAuditStatusEnum.REJECTED,
                ResourceAuditStatusEnum.HIDDEN: ResourceAuditStatusEnum.HIDDEN,
            },
        ),
        AuditFlow("report", _ReportMachine, "reportStatus", affects_primary=report_affects),
    ]
    return AuditFlowOrchestrator(primary=primary, flows=flows)


async def test_resource_has_multiple_independent_flows(session):
    """一资源挂多环节，各自独立流转互不干扰。"""
    primary = _resource(ResourceAuditStatusEnum.AUDITING)
    orch = _make_orchestrator(primary)

    # 内容环节：审核通过
    content_row = _FlowRow("auditStatus", ResourceAuditStatusEnum.AUDITING)
    await orch.run_flow(session, content_row, "content_manual",
                        action=ModerationAction.APPROVE, actor_mid=1)
    assert getattr(content_row, "auditStatus") is ResourceAuditStatusEnum.NORMAL
    # 主状态联动为 NORMAL
    assert primary.auditStatus is ResourceAuditStatusEnum.NORMAL

    # 举报环节（独立，affects_primary=False）：单独流转不影响主状态
    report_row = _FlowRow("reportStatus", ResourceAuditStatusEnum.AUDITING)
    await orch.run_flow(session, report_row, "report",
                        action=ModerationAction.REJECT, actor_mid=2)
    assert getattr(report_row, "reportStatus") is ResourceAuditStatusEnum.REJECTED
    # 主状态仍为 NORMAL，未被举报驳回覆盖
    assert primary.auditStatus is ResourceAuditStatusEnum.NORMAL


async def test_affects_primary_linked_via_when_primary(session):
    """affects_primary 环节按 when_primary 联动主状态。"""
    primary = _resource(ResourceAuditStatusEnum.AUDITING)
    orch = _make_orchestrator(primary)

    content_row = _FlowRow("auditStatus", ResourceAuditStatusEnum.AUDITING)
    await orch.run_flow(session, content_row, "content_manual",
                        action=ModerationAction.REJECT, actor_mid=1, reason="违规")
    # 内容环节 REJECTED -> 主 REJECTED
    assert primary.auditStatus is ResourceAuditStatusEnum.REJECTED


async def test_primary_only_written_by_orchestrator_single_entry(session):
    """主状态单一入口：环节机器不直接写主表，联动全由编排器 when_primary 决定。"""
    primary = _resource(ResourceAuditStatusEnum.AUDITING)
    orch = _make_orchestrator(primary)

    # 内容环节机器 run 前，其 state_attr 被编排器临时指到环节记录上，不碰主对象
    content_row = _FlowRow("auditStatus", ResourceAuditStatusEnum.AUDITING)
    await orch.run_flow(session, content_row, "content_manual",
                        action=ModerationAction.HIDE, actor_mid=1)
    assert primary.auditStatus is ResourceAuditStatusEnum.HIDDEN


async def test_unknown_flow_raises(session):
    primary = _resource(ResourceAuditStatusEnum.AUDITING)
    orch = _make_orchestrator(primary)
    with pytest.raises(KeyError):
        await orch.run_flow(session, _FlowRow("x", ResourceAuditStatusEnum.AUDITING), "nope",
                            action=ModerationAction.APPROVE, actor_mid=1)


async def test_illegal_flow_transition_raises_and_primary_unchanged(session):
    """环节内非法流转抛 StateTransitionError，且不改主状态。"""
    primary = _resource(ResourceAuditStatusEnum.NORMAL)
    orch = _make_orchestrator(primary)

    # 举报环节从 REJECTED 无法 APPROVE（_REPORT_TRANSITIONS 无 REJECTED->NORMAL）
    report_row = _FlowRow("reportStatus", ResourceAuditStatusEnum.REJECTED)
    with pytest.raises(StateTransitionError):
        await orch.run_flow(session, report_row, "report",
                            action=ModerationAction.APPROVE, actor_mid=1)
    assert primary.auditStatus is ResourceAuditStatusEnum.NORMAL


async def test_has_flow_helper(session):
    primary = _resource(ResourceAuditStatusEnum.AUDITING)
    orch = _make_orchestrator(primary)
    assert orch.has_flow("content_manual") is True
    assert orch.has_flow("report") is True
    assert orch.has_flow("text_auto") is False
