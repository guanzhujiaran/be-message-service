"""RPA 插件（``InteractionBizTypeEnum.RPA_PLUGIN``）seed —— **待实现**。

目标链路（RPA-Browser ``/plugins/*``）：

1. 注册插件（``/plugins/create``）→ 拿 ``plugin_id``；
2. 列表 / 详情回查（``/plugins/list``）断言已落库；
3. 更新（``/plugins/update``）与 Fork（``/plugins/fork``）覆盖版本分支；
4. be-message 侧以 ``(RPA_PLUGIN, plugin_id)`` 跑通用互动与审核覆盖（与 ``action.py`` 同套链路）；
5. 删除（``/plugins/delete``）后回查不可见。

实现时在本模块提供 ``async def seed_rpa_plugins(client, users, ...)``，
接口方法加在 ``seed/client.py`` 的 RPA 分段，再由 ``seed/cli.py`` 挂进编排。
"""
