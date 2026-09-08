"""RPA 提交审批（工作流 ``RPA_WORKFLOW`` + 审批单）seed —— **待实现**。

目标链路（RPA-Browser ``/approval/*`` + ``/workflows/*``）：

1. 创建工作流（``/workflows/create``）→ 拿 ``workflow_id``；
2. 提交审批（``/approval/submit``）：资源以 ``(RPA_WORKFLOW, workflow_id)`` 定位，
   提交后状态应为待审（对齐 be-message ``ResourceAuditStatusEnum.AUDITING``）；
3. 审批列表回查（``/approval/list``）断言审批单可见；
4. 管理员审批（``/approval/review``）覆盖**通过 / 驳回**两条流转，并回查资源状态与审核流水
   （``TResourceAuditLog``）；
5. 取消审批（``/approval/cancel``）覆盖发起人撤回单据。

实现时在本模块提供 ``async def seed_rpa_approval(client, users, ...)``，
接口方法加在 ``seed/client.py`` 的 RPA 分段，再由 ``seed/cli.py`` 挂进编排。
"""
