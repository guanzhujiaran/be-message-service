"""RPA 资源 seed（**待接入**）。

对接 RPA-Browser 服务，覆盖 ``InteractionBizTypeEnum`` 的 RPA 系列资源：

| 模块 | 资源 | 对接接口（RPA-Browser） | 覆盖链路 |
| --- | --- | --- | --- |
| ``action.py`` | ``RPA_ACTION``（自定义操作） | ``/custom_actions/{create,list,get,update,delete,fork}`` | 创建 → 列表/详情回查 → 更新 → Fork → 删除；并在 be-message 侧跑通用互动（点赞/评论/@/举报/审核） |
| ``plugin.py`` | ``RPA_PLUGIN``（插件） | ``/plugins/{create,list,update,delete,fork}`` | 注册 → 列表/详情回查 → 更新 → Fork → 删除；插件作为通用资源的互动与审核覆盖 |
| ``approval.py`` | ``RPA_WORKFLOW`` + 审批单 | ``/approval/{submit,list,review,cancel}`` | 工作流/资源提交审批 → 审批列表回查 → 通过/驳回 → 取消；与 be-message 审核状态机（``ResourceAuditStatusEnum``）对齐 |

**接入约定**（与既有 seed 一致）

1. 一律经 HTTP 接口（走 be-gateway / RPA-Browser），**不直写库**；
2. 接口方法加在 ``seed/client.py`` 的对应分段（RPA 分段），断言加在 ``seed/verify.py``；
3. 场景函数签名与 ``scenarios/`` 现有场景一致：``async def seed_xxx(client, users, ...)``；
4. 在 ``seed/cli.py`` 增加 ``--rpa-base-url`` / ``--skip-rpa`` 等开关并挂进两阶段编排；
5. 断言失败按「响亮报错（暴露 bug）/ 软降级（外部依赖不可用）」分档。
"""
