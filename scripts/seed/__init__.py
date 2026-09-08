"""统一 seed 包（纯 HTTP 接口调用版）。

入口仍是唯一的 ``scripts/seed_cli.py``（只做 sys.path 注入 + 调 :func:`seed.cli.main`）：

    uv run python scripts/seed_cli.py                  # 全互动联调 + 大数据灌数
    uv run python scripts/seed_cli.py --skip-bulk       # 仅全互动联调
    uv run python scripts/seed_cli.py --skip-full       # 仅大数据灌数
    uv run python scripts/seed_cli.py --dry-run         # 只打印计划

**目录结构（按功能划分）**

| 模块 | 职责 |
| --- | --- |
| ``cli.py`` | argparse 参数 + 两阶段编排（``_run_full`` / ``_run_bulk``）+ ``main()`` |
| ``config.py`` | HTTP 超时 / 请求并发上限 / 举报原因 / 资源池容量等全局常量 |
| ``rr.py`` | 确定性轮遍游标 ``_rr`` 与分布采样 ``_sample`` |
| ``helpers.py`` | ``x-bili-*`` 请求头、@ 文本与 AT 节点、富文本正文构造 |
| ``material.py`` | 素材池（正文/评论语/图片/话题名）+ 外库加载 |
| ``datasource.py`` | 外库**只读**数据源（biliopusdb / bilidb / pptr 用户池 / 主库已有话题） |
| ``probes.py`` | be-message 只读探针（私信设置 / 会话关系 / 私信索引 / 私信配对） |
| ``client.py`` | ``SeedClient``：按功能域分段的 HTTP 薄封装 |
| ``verify.py`` | @ 落库渲染 / AT 事件触达 / 转发计数状态机断言 |
| ``blocklist.py`` | 黑名单归一（每人 ≤1 条）+ 私信配对避让 |
| ``lottery.py`` | 真实 lottery_id 获取 + 动态/lottery 混合资源池 |
| ``full.py`` | 阶段一编排 ``seed()`` |
| ``scenarios/`` | 阶段一场景：动态 / 评论 / 用户级互动 / 消息与管理 / 管理侧动作 |
| ``bulk/`` | 阶段二大数据灌数：分布常量 / 话题 / 动态 / 评论套件 / 私信 + 编排 |
| ``rpa/`` | **待接入**：RPA 操作 / 插件 / 提交审批 |

**两阶段说明**

- 阶段一「全互动联调」（4 大类 18 项，纯接口调用、断言失败响亮报错）：动态体系
  （话题/发布/审核/点赞/转发/浏览/举报/置顶）、评论体系（一级评论/楼中楼/赞踩/@/举报/置顶）、
  用户级互动（收藏夹/收藏/关注/拉黑/事件通知/系统通知）、消息与管理
  （私信发送/审核/撤回/删除全流程、通用计数、用户举报、封禁、头像审核流）。
- 阶段二「大数据灌数」（基于 biliopusdb 真实动态 + crawler 真实 lottery + pptr 用户池私信，
  走 API、软降级跳过）：批量灌动态 + 点赞/评论/@/浏览 + lottery 评论套件 + 批量私信。

**共同约定**

- 全部走 be-message 真实 HTTP 业务链路（``x-bili-*`` 头模拟网关身份；root 审核统一走
  ``--admin-mid``），不直接写 MySQL 主库；仅只读回查 pptr Postgres / be-message /
  biliopusdb 确认前置数据存在。
- 作者/互动者取自 pptr Postgres 真实用户（只读）。
- 阶段一断言失败**响亮报错**（暴露代码 bug）；阶段二（biliopusdb/连接问题）**软降级跳过**。
- @ 提及在动态正文（AT 节点）与评论正文（``@昵称`` + ``at_name_to_mid``）统一追加，
  落库后回查断言渲染与 AT 事件触达。

新增 seed 场景时：按功能在对应包内加模块 + 场景函数，再由 ``cli.py`` 增加
``--skip-*`` 开关挂进编排；不允许把逻辑堆回单文件。
"""
