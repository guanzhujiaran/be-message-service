"""RPA 自定义操作（``InteractionBizTypeEnum.RPA_ACTION``）seed —— **待实现**。

目标链路（RPA-Browser ``/custom_actions/*``）：

1. 创建自定义操作（``/custom_actions/create``）→ 拿 ``action_id``；
2. 列表 / 详情回查（``/custom_actions/list``、``/custom_actions/get``）断言已落库；
3. 更新（``/custom_actions/update``）与 Fork（``/custom_actions/fork``）覆盖版本分支；
4. be-message 侧以 ``(RPA_ACTION, action_id)`` 跑通用互动：点赞 / 评论（含 @、楼中楼）/
   举报 / 审核，并验证 LIKE 事件需显式补发（通用资源后端不自动生成）；
5. 删除（``/custom_actions/delete``）后回查不可见。

实现时在本模块提供 ``async def seed_rpa_actions(client, users, ...)``，
接口方法加在 ``seed/client.py`` 的 RPA 分段，再由 ``seed/cli.py`` 挂进编排。
"""
