# 用户注销冷静期（两阶段注销 + Casdoor 同步删除）实施计划

## 背景与目标

当前注销是「立即物理删除」：`/deactivate`、`/admin/deactivate` 投递 MQ → consumer
调 `PptrUser.deactivate()`，物理删除 pptr Postgres 四表 + be-message MySQL 业务数据，
**全程不碰 Casdoor**（[base.py#L1226-L1280](file:///home/minato/BilibiliExplosion/be-message-service/app/services/user/account/base.py#L1226-L1280)）。
导致本地账号已删、Casdoor 中心账号仍可登录的不一致。

改为**两阶段注销**：

1. 提交注销 → 进入 **N 天冷静期（默认 7，可配置）**，仅标记冻结，数据全部保留，可恢复。
2. 冷静期内：存量 JWT 访问被拒；**重新走 Casdoor OAuth 登录即自动撤销**（清除冻结标记，恢复账号）。Casdoor 在冷静期内保持不动（不禁用、不删除），否则用户无法登录触发自动撤销。
3. 到期未撤销 → 定时任务物理删除本地全部数据（复用现有清理逻辑），再调 Casdoor `delete-user` 删除中心账号；Casdoor 删除失败不回滚本地，落「待补偿」记录由重试任务补偿 + 告警。

自助注销与管理员注销都走冷静期。

## Repository Research（关键事实）

- 用户主体 `TUserInfo/TUserDetail/TUserLevel/TUserVip` 在 **pptr Postgres**
  （[pptr_db.py](file:///home/minato/BilibiliExplosion/be-message-service/app/models/pptr_db.py)）。
  **硬约束：只改 MySQL，pptr Postgres 不改结构**。因此冷静期状态不能加列到 `TUserInfo`，
  需在 **MySQL 新建一张注销状态表**承载（现有 `msg_user_setting`/`msg_user_activity`/
  `msg_admin` 语义均不符，不复用）。
- 登录回调 `GET /api/v1/user/casdoor/callback`
  （[pptr_user_gateway.py#L948-L1100](file:///home/minato/BilibiliExplosion/be-message-service/app/api/pptr_user_gateway.py#L948-L1100)）
  在 pptr session 内调 `casdoor_service.create_local_user_from_casdoor(...)`，拿到
  `local_user.uid`，随后签发 JWT。**自动撤销的挂载点**：拿到 uid 后、签发 JWT 前，
  查 MySQL 注销状态表，若处于冷静期则清除标记。
- 认证依赖 `get_current_user`
  （[user.py#L36-L79](file:///home/minato/BilibiliExplosion/be-message-service/app/dependencies/user.py)）
  只从网关注入的 `x-bili-*` 头 / JWT 还原 `AuthInfo`，不查库。生产流量经 be-gateway
  `/identify`。**冷静期存量 JWT 拦截**最稳妥放在 be-gateway `/identify`（已查库、缓存键
  为 JWT 签名），但本计划范围为 be-message；be-message 侧提供查询能力并在本服务关键写
  接口加一道依赖拦截，跨服务网关拦截列为关联项（见 Dependencies）。
- 定时框架：APScheduler `AsyncIOScheduler`
  （[scheduler.py](file:///home/minato/BilibiliExplosion/be-message-service/app/tasks/scheduler.py)），
  所有 job 统一 `max_instances=1 + coalesce=True + misfire_grace_time=60`，在
  `start_scheduler()` 注册，main lifespan 启动。
- 配置：pydantic-settings `Settings`
  （[config.py](file:///home/minato/BilibiliExplosion/be-message-service/app/core/config.py)），
  直接加字段即可被环境变量覆盖。
- Casdoor：`AsyncCasdoorSDK.delete_user(user)` 已存在
  （`.venv/.../casdoor/async_main.py#L462`，POST `/api/delete-user`，需要含
  owner/name 的 `User` 对象）。用 **admin SDK + admin token** 调（与现有
  `_get_admin_sdk()`/`_get_admin_access_token()` 一致）。
- 现有立即删除链路：`publisher.publish_user_deactivate(uid)`（rk=`message.user.deactivate`）
  → [consumers/deactivate.py](file:///home/minato/BilibiliExplosion/be-message-service/app/consumers/deactivate.py)
  → `PptrUser.deactivate()`。改造后该 MQ/consumer 不再用于「立即删」，
  改为由定时任务驱动；MQ 相关代码保留（见步骤 7 处理）。
- MySQL 最新迁移 revision = `20260920_stat_viewlog_enum`
  （`alembic/versions/20260920_2230-stat_viewlog_enum_.py`），新迁移 down_revision 指向它。
  主库启动自动 `alembic upgrade head`（`alembic_auto_migrate`）。

## 数据模型（新建 MySQL 表 `msg_user_deactivation`）

字段：
- `id` BIGINT 主键自增。
- `mid` BIGINT NOT NULL，UNIQUE（一个用户同时只有一条有效注销记录），index。
- `user_name` VARCHAR(128) NULL（记录 Casdoor name，供到期删除构造 User；防止中途本地
  user_name 不可读）。
- `status` VARCHAR(16) NOT NULL，default `'cooling'`，index：
  - `cooling`：冷静期，可撤销；
  - `pending_casdoor`：本地已物理删、Casdoor 待删（补偿态）。
- `deactivated_at` DATETIME NOT NULL（提交时刻）。
- `delete_after` DATETIME NOT NULL，index（= deactivated_at + N 天，到期扫描键）。
- `casdoor_delete_error` VARCHAR(512) NULL（最近一次删除失败原因，便于排查）。
- `retry_count` INT NOT NULL default 0。
- 复用 `TimestampMixin`（createdAt/updatedAt，遵循 onupdate 约定）。

物理删除本地成功后把该记录 status 置为 `pending_casdoor`（不删行，作为 Casdoor 删除的
补偿凭证）；Casdoor 删除成功后删除该记录（或置 `done`，二选一，计划采用**删除记录**保持表小）。

## Files and Modules

- `app/models/db/deactivation_tbl.py`（新建）：`UserDeactivation` 模型。
- `app/models/db/__init__.py`：导出 `UserDeactivation`。
- `alembic/versions/<date>_user_deactivation.py`（新建）：主库建表迁移，
  down_revision=`20260920_stat_viewlog_enum`。
- `app/services/user/deactivation_service.py`（新建）：
  - `submit_deactivation(session, mid, user_name) -> UserDeactivation`（幂等：已 cooling 则刷新/沿用）。
  - `is_deactivated(session, mid) -> bool`、`get_cooling(session, mid)`。
  - `auto_revoke_if_cooling(session, mid) -> bool`（登录自动撤销：cooling 则删记录）。
  - `list_due(session, now, limit)`（status=cooling 且 delete_after<=now）。
  - `list_pending_casdoor(session, limit)`（补偿扫描）。
  - `mark_pending_casdoor(...)`、`clear_after_casdoor_deleted(...)`、`mark_casdoor_error(...)`。
- `app/services/user/casdoor_service.py`：新增
  `delete_casdoor_user(*, user_name) -> None`（用 admin sdk + admin token，构造最小
  `User(owner=org, name=user_name)` 调 `sdk.delete_user`；失败抛异常，由任务层捕获）。
  遵循项目约束：不在 service 内 try-catch 吞错。
- `app/services/user/account/base.py`：
  - 现有 `deactivate()` 拆为可复用：保留 `_delete_pptr_user()` +
    `_delete_message_data()` 的物理清理（供到期任务调用）；
  - 新增 `purge_expired()` 编排（或放在任务层调用这两个私有方法 + Casdoor 删除）。
- `app/api/pptr_user_gateway.py`：
  - `/deactivate`、`/admin/deactivate` 改为：写 MySQL 注销状态（cooling，
    delete_after=now+N天）→ 不再投递立即删除 MQ；返回「已提交，N 天内重新登录可撤销」。
  - `casdoor_callback`：拿到 `local_user.uid` 后调用
    `auto_revoke_if_cooling`（新开 MySQL session，与 pptr session 分离），撤销后记日志。
  - 视需要新增 `GET /deactivation/status`（供前端展示剩余冷静期/是否可恢复）——本期可选。
- `app/dependencies/user.py`：新增 `ActiveUser` 依赖（在 `get_current_user` 基础上查
  MySQL，cooling 状态抛 403「账号已注销，重新登录可恢复」），用于本服务敏感写接口；
  `get_current_user` 本身保持纯头解析不变（避免给所有只读/可选接口加库查询）。
- `app/tasks/scheduler.py`：新增两个 job 并注册：
  - `deactivate_expire_job`（间隔可配置，默认 3600s）：扫 due → 逐用户：物理删本地
    （pptr + MySQL）→ 同事务置 `pending_casdoor` 提交 → 调 Casdoor 删除：
    成功删记录；失败 `mark_casdoor_error`+retry_count+1，等补偿 job。
  - `casdoor_delete_retry_job`（默认 3600s）：扫 `pending_casdoor` 重试 Casdoor 删除，
    超过阈值记 ERROR 告警日志。
- `app/core/config.py`：新增
  - `account_deactivate_grace_days: int = 7`
  - `deactivate_expire_scan_seconds: int = 3600`
  - `casdoor_delete_max_retry: int = 10`
  - 复用 `scheduler_enabled` 总开关。
- `app/mq/consumers/deactivate.py` + publisher：注销 MQ 不再用于立即删。
  计划：保留路由/消费者但让其成为「无操作或转发到 submit 语义」的兼容层，或在同 PR 删除其注册。
  **采用：停止在 deactivate 接口投递该消息；consumer 代码保留但不再有生产者**（避免动 FastStream 注册面过大），并在代码注释标注废弃，后续单独清理。

## Implementation Steps（依赖顺序）

1. 新建 `UserDeactivation` 模型 + `__init__` 导出。
2. 新建 Alembic 主库迁移（仅 MySQL），本地 `alembic upgrade head` 验证建表。
3. config 增加冷静期/扫描/重试配置项。
4. 实现 `deactivation_service.py`（提交/查询/撤销/到期扫描/补偿态流转）。
5. `casdoor_service.py` 增加 `delete_casdoor_user`。
6. 改造两个注销 API 为「写冷静期标记」，更新响应文案；停止投递立即删除 MQ。
7. 改造 `casdoor_callback`：签发 JWT 前自动撤销。
8. 增加 `ActiveUser` 依赖并在敏感写接口启用（先接注销/资料/发布类关键路由，只读不改）。
9. scheduler 增加到期物理删除 job 与 Casdoor 补偿 job 并注册。
10. 处理旧 deactivate consumer 的废弃标注。
11. 自检：py_compile、venv 导入、迁移 up/down、关键路径走查。

## Dependencies and Considerations

- **pptr 与 MySQL 跨库无事务**：到期删除里「pptr 物理删」「MySQL 业务删」「注销状态置
  pending_casdoor」无法同一事务。编排顺序：先删 pptr + MySQL 业务数据，再把 MySQL
  注销记录置 pending_casdoor 并提交；若进程在中间崩溃，补偿/下次扫描需**幂等**
  （物理删对「已无数据」返回 0 行正常，现有 deactivate 已幂等）。
- **Casdoor 删除最终一致**：本地数据删除不以 Casdoor 成功为前提；Casdoor 失败只落补偿态，
  不回滚本地（避免已应删账号复活 + 大批量删除重放）。
- **登录自动撤销的可靠性**：`create_local_user_from_casdoor` 与撤销分别在 pptr / MySQL
  两个 session；撤销失败不应阻断登录（catch 记录错误并放行更安全，避免用户锁死），但
  到期 job 仍可能在边界时刻删除——属极低概率，靠 delete_after 与登录时刻的先后天然规避。
- **be-gateway `/identify` 与 Redis 缓存**：网关缓存身份 5 分钟，冷静期内被拉黑的存量
  JWT 在缓存 TTL 内可能仍被 /identify 放行。**关联项（be-gateway 改造，超出本计划文件
  所在服务）**：需要在 be-message `/identify` 查注销状态并拒绝，或注销时让网关失效对应
  缓存。本计划在 be-message 侧提供 `is_deactivated` 查询并在本服务写接口拦截；网关联动
  作为后续项，实施时与你确认是否同批做。
- 管理员注销同样进入冷静期；若需要「管理员立即彻底删除」，本期不提供（保持简单）。
- SDK 属自动生成/三方代码：不修改 casdoor 包，仅调用其 `delete_user`。

## Validation

- `python -m py_compile` 所有改动文件；venv 内 import 模型/服务/路由无误。
- 主库 Alembic `upgrade head` 建表成功、`downgrade -1` 回滚成功（pptr 库无变更）。
- 单测/脚本（tests 下新增或临时脚本，跑完即删）：
  1. submit：生成 cooling 记录，delete_after = now+7d。
  2. auto_revoke：模拟回调，记录被清除、再次登录放行。
  3. expire：把 delete_after 手工调到过去，跑 job，断言本地表清空 + 记录转
     pending_casdoor（可 mock Casdoor 删除失败验证补偿态）。
  4. Casdoor 删除成功路径（在测试环境或 mock admin sdk）断言记录被清除。
- scheduler 在 `SCHEDULER_ENABLED=false` 时不注册（沿用现有开关）。

## Risks

- 网关存量 JWT 短期仍可用（缓存 + 本服务只读接口未全部加拦截）：通过 /identify 联动 +
  敏感写接口 `ActiveUser` 兜底；接受只读侧最长 5 分钟的宽限。
- Casdoor 长时间不可用导致 pending_casdoor 堆积：retry job + ERROR 告警 +
  `casdoor_delete_max_retry` 上限后保持告警不静默。
- 到期 job 批量删除压力：逐用户处理、单个用户独立提交，复用现有分批/幂等清理，
  `max_instances=1` 防并发。
