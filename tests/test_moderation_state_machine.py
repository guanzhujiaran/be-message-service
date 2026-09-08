"""统一审核状态机骨架单测（阶段一，纯单元测试，不依赖数据库）。

覆盖契约（见 `app/services/moderation/state_machine.py`）：
- 合法迁移写状态列；
- 非法迁移抛 `StateTransitionError`（迁移表唯一拦截点）；
- `on_enter` 副作用被调用、顺序正确、参数透传；
- `to_native` / `native_state_of` 默认恒等映射；
- `_write_audit_log` 可作为钩子覆盖并收到完整上下文。
"""
import pytest

from app.services.moderation.state_machine import (
    AuditStateMachine,
    ModerationAction,
    ResourceAuditStatusEnum,
    StateTransitionError,
    Transition,
)


class _FakeRow:
    """模拟待审核对象：只含审核态列与业务标识。"""

    def __init__(self, biz_id: int, audit_status: ResourceAuditStatusEnum):
        self.biz_id = biz_id
        self.auditStatus = audit_status


class _FakeSession:
    """桩 session，记录副作用 / 流水写入顺序。"""

    def __init__(self):
        self.calls: list[str] = []


class _MomentMachine(AuditStateMachine):
    """最小可用子类：模拟"实体生命周期"合法流转 + 副作用钩子。"""

    state_attr = "auditStatus"

    TRANSITIONS = (
        Transition(ResourceAuditStatusEnum.AUDITING, ModerationAction.APPROVE, ResourceAuditStatusEnum.NORMAL),
        Transition(ResourceAuditStatusEnum.REJECTED, ModerationAction.APPROVE, ResourceAuditStatusEnum.NORMAL),
        Transition(ResourceAuditStatusEnum.AUDITING, ModerationAction.REJECT, ResourceAuditStatusEnum.REJECTED),
        Transition(ResourceAuditStatusEnum.NORMAL, ModerationAction.REJECT, ResourceAuditStatusEnum.REJECTED),
        Transition(ResourceAuditStatusEnum.NORMAL, ModerationAction.HIDE, ResourceAuditStatusEnum.HIDDEN),
        Transition(ResourceAuditStatusEnum.AUDITING, ModerationAction.HIDE, ResourceAuditStatusEnum.HIDDEN),
    )

    def native_state_of(self, row: _FakeRow) -> ResourceAuditStatusEnum:
        return ResourceAuditStatusEnum(int(row.auditStatus))

    async def on_enter(self, session, row, new_state, *, action, actor_mid, reason, remark):
        session.calls.append(f"on_enter:{new_state.name}")

    async def _write_audit_log(self, session, row, *, from_state, to_state, action, actor_mid, reason, remark):
        session.calls.append(
            f"audit:{from_state.name}->{to_state.name}:{action.name}:actor={actor_mid}:reason={reason}"
        )


@pytest.fixture
def session():
    return _FakeSession()


async def test_legal_transition_writes_state_and_returns(session):
    """合法流转：写状态列 + 返回目标态 + 副作用与流水顺序正确。"""
    row = _FakeRow(biz_id=100, audit_status=ResourceAuditStatusEnum.AUDITING)
    machine = _MomentMachine()

    to = await machine.transition(
        session, row, action=ModerationAction.APPROVE, actor_mid=7, reason=None, remark="pass"
    )

    assert to is ResourceAuditStatusEnum.NORMAL
    assert row.auditStatus is ResourceAuditStatusEnum.NORMAL
    # on_enter 先于 _write_audit_log
    assert session.calls[0] == "on_enter:NORMAL"
    assert session.calls[1] == "audit:AUDITING->NORMAL:APPROVE:actor=7:reason=None"


async def test_illegal_transition_raises(session):
    """非法流转：HIDDEN 状态下无法 APPROVE，抛 StateTransitionError 且不改状态。"""
    row = _FakeRow(biz_id=1, audit_status=ResourceAuditStatusEnum.HIDDEN)
    machine = _MomentMachine()

    with pytest.raises(StateTransitionError) as ei:
        await machine.transition(session, row, action=ModerationAction.APPROVE, actor_mid=7)

    assert ei.value.from_state is ResourceAuditStatusEnum.HIDDEN
    assert ei.value.action is ModerationAction.APPROVE
    # 状态未被污染
    assert row.auditStatus is ResourceAuditStatusEnum.HIDDEN
    assert session.calls == []


async def test_reject_then_reapprove_allowed(session):
    """驳回后重审合法（AUDITING->REJECTED->NORMAL 全路径）。"""
    row = _FakeRow(biz_id=1, audit_status=ResourceAuditStatusEnum.AUDITING)
    machine = _MomentMachine()

    await machine.transition(session, row, action=ModerationAction.REJECT, actor_mid=7, reason="bad")
    assert row.auditStatus is ResourceAuditStatusEnum.REJECTED

    await machine.transition(session, row, action=ModerationAction.APPROVE, actor_mid=8, remark="recheck")
    assert row.auditStatus is ResourceAuditStatusEnum.NORMAL


async def test_default_native_state_of_is_identity_with_abstract_guard():
    """默认 `to_native` 恒等；`native_state_of` 必须由子类实现（抽象方法约束）。"""
    machine = _MomentMachine()
    assert machine.to_native(ResourceAuditStatusEnum.NORMAL) is ResourceAuditStatusEnum.NORMAL

    # 直接实例化抽象基类应失败
    with pytest.raises(TypeError):
        AuditStateMachine()  # type: ignore[abstract]
