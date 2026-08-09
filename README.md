# be-message-service

统一消息系统微服务。基于 **FastAPI + FastStream(RabbitMQ) + SQLModel + Alembic + APScheduler**，
承载站内四大消息模块，并集中接管 `be-bilibili-crawler` / `RPA-Browser` 的外部推送能力。

**存储只用 MySQL，不依赖 Redis**：活跃度、游标、未读数全部走表 + 索引，
私信正文按月分库 + 库内 100 张分表。

---

## 一、模块划分

| 模块 | 路由前缀 | 扩散模型 | 关键机制 |
| --- | --- | --- | --- |
| 系统通知 `notify` | `/api/v1/message/notify` | **读扩散** | 全站一份内容 + 每用户游标（`msg_notify_cursor`）增量拉取，天然去重 |
| 事件提醒 `event` | `/api/v1/message/event` | **写扩散** | 按接收者落行；`idx_event_group` 聚合展示；`dedup_key` 唯一索引做幂等 |
| 私信 `dm` | `/api/v1/message/dm` | **写扩散** | 收发双方各一份索引/会话；正文经 MQ 异步落分片；死信表补偿 |
| 消息设置 `setting` | `/api/v1/message/setting` | — | 整个系统的第一道闸门：上报 / 投递 / 推送前先查开关与免打扰时段 |
| 聚合入口 `msg_feed` | `/api/v1/message/msg_feed` | — | 跨模块未读汇总 + 活跃心跳，避免前端为一个红点发四个请求 |
| 外部推送 `push` | `/api/v1/message/push` | — | **站外提醒**专用：PushMe / PushPlus / SMTP / Bark / 钉钉 / 飞书 / Ntfy / WxPusher 等渠道（与站内信解耦） |

### 为什么通知用读扩散、事件和私信用写扩散

- 系统通知：**量级小、受众广**。按用户写扩散会产生 N 倍冗余行；改成「一份内容 + 用户游标」，
  写入 O(1)，读取只捞 `id > last_notify_id` 的增量，重复调用 `/pull` 不会拿到重复数据。
- 事件提醒 / 私信：**量级大、受众窄（1~2 人）**。写扩散把读路径压成单表单索引扫描，
  不需要 JOIN、不需要反查会话，深翻页也不退化。单聊写放大系数固定为 2，完全可接受。

---

## 二、推送策略（站内信）

> ⚠️ 站内信 ≠ 第三方推送：系统通知 / 事件提醒 / 私信都属于**站内信**，送达完全由
> 数据库写路径保证（通知读扩散、事件 / 私信写扩散），用户经 `/pull` `/list`
> `/messages` `msg_feed` 轮询感知，**不经由任何第三方推送渠道**（PushMe / PushPlus 等）。
> 第三方推送是**独立于站内信**的能力，仅由 `/api/v1/message/push` 用于「站外提醒」类内容。

```
事件产生 → 消息设置闸门 → 落库（站内信已送达）
                                  └─ 用户关了该类提醒 → SKIPPED（不落库）
```

- **送达方式**：写路径落库后即对用户可见；前端在消息中心轮询 `POST /msg_feed/heartbeat`
  维持活跃态并按需拉取（游标去重），无需服务端实时推。
- **免打扰**：`can_push_now()` 命中 `dnd_start_hour ~ dnd_end_hour` 时，相关事件 / 私信
  不落库（对接收方不可见），免打扰结束后恢复。
- **第三方推送为独立链路**：仅 `/api/v1/message/push` 使用，端点故障只告警不阻断主流程，
  `MESSAGE_CONFIG` 为空时静默降级，与站内信送达互不影响。

---

## 三、私信分表路由

`msgkey` 是内嵌毫秒时间戳的 64 位雪花 ID（`msgkey_epoch_ms` 起算，`msgkey_worker_id` 区分实例）。
路由完全由 msgkey 自解释，**不需要回查数据库**：

```
库名  = {dm_content_db_prefix}_{YYYYMM}     # 由 msgkey 内嵌时间戳解析，如 bili_msg_content_202608
表名  = {dm_content_table_prefix}_{00..99}  # msgkey % dm_content_table_count
```

- 主库（`BiliMessageDB`）只存**索引与会话**（`msg_dm_index` / `msg_dm_session`），字段轻量。
- 正文投递到 MQ 由 `consume_dm_content` 异步写入分片：发送接口 RT 不受正文长度和建表 DDL 影响。
- 索引行冗余 `content_preview` + `content_ready`：分片未就绪时读接口用摘要兜底，用户无感知。
- 写分片失败落 `msg_dm_content_dlq`，由 `retry_dead_letter_job` 重试补偿，保证正文最终一致。
- 分片库**不由 Alembic 管理**，运行时懒创建；`prewarm_shard_job` 会跨月提前建好下个月的 100 张表。
- **msgkey 一律以字符串传输**：64 位整数直接给浏览器会触发 JS `Number.MAX_SAFE_INTEGER` 精度丢失。

---

## 四、枚举落库约定

| 类型 | 列类型 | 存储内容 | 工具 |
| --- | --- | --- | --- |
| `StrEnum` | `VARCHAR(n)` | **value**（`like` / `published` / `stranger`） | `str_enum_type()` |
| `IntEnum` | `INTEGER` | **value**（0 / 1 / 2） | `int_enum_type()` |

一律**不使用 MySQL 原生 ENUM**：新增枚举值不需要 DDL 改表，且库内字面量与接口层完全一致。
SQLModel 的默认行为会落成原生 ENUM 且存**成员名**（`LIKE`），一旦回退会导致查询静默全错，
因此有专门的回归测试 `tests/test_phase_e_enums.py` 守住这条约定。

---

## 五、定时任务

| 任务 | 间隔 | 作用 |
| --- | --- | --- |
| `dispatch_notify_job` | `notify_dispatch_interval_seconds`（60s） | 捞 `dispatched=False` 的已发布通知 → 标记 `dispatched=True`（读扩散，发布即对用户可见，仅维护已投递状态） |
| `retry_dead_letter_job` | 固定 | 重试补写私信正文死信 |
| `prewarm_shard_job` | 固定 | 跨月提前创建下个月的内容分表 |

---

## 六、运维开关

| 环境变量 | 默认 | 说明 |
| --- | --- | --- |
| `SCHEDULER_ENABLED` | `true` | 设为 `false` 可**暂停全部后台定时任务**（只保留 REST + MQ 消费），用于排障或多实例只留一个跑调度 |
| `ALEMBIC_AUTO_MIGRATE` | `true` | 启动时自动 `alembic upgrade head`；设为 `false` 改为手动迁移（生产灰度时建议关掉） |
| `ALEMBIC_UPGRADE_TARGET` | `head` | 自动迁移的目标 revision |
| `MYSQL_MESSAGE_URL` | — | 消息系统主库连接串，库不存在时由 `ensure_database()` 自动 `CREATE DATABASE` |
| `RABBITMQ_URL` | — | RabbitMQ 连接串 |
| `MESSAGE_CONFIG` | `{}` | 全局推送渠道配置（JSON），与 `be-bilibili-crawler` / `rpa-browser` 共用同一份 |
| `MSGKEY_WORKER_ID` | `1` | 雪花 ID 的实例编号，**多实例部署时必须互不相同**（0~1023） |
| `ACTIVE_USER_WINDOW_SECONDS` | `300` | 活跃判定窗口（仅用于前端轮询节奏，与消息送达无关） |
| `DM_RECALL_WINDOW_SECONDS` | `120` | 私信可撤回时间窗 |
| `DM_CONTENT_SYNC_FALLBACK` | `true` | MQ 不可用时正文降级为同步落库 |

### 常见运维动作

```bash
# 暂停后台任务（滚动重启时避免多实例重复投递）
docker compose up -d -e SCHEDULER_ENABLED=false be-message-service

# 关掉自动迁移，改手工执行
ALEMBIC_AUTO_MIGRATE=false docker compose up -d be-message-service
docker compose exec be-message-service uv run alembic upgrade head

# 健康检查（204=存活且 broker 连通，503=broker 未连）
curl -i http://localhost:18739/health
```

---

## 七、启动链路

`app/main.py` 的 lifespan 依次执行：

```
依赖自检（RabbitMQ + MySQL 为强依赖，失败即退出；推送渠道为弱依赖，仅告警）
  → ensure_database() 自动建库
  → alembic upgrade head（alembic_auto_migrate=True 时）
  → ensure_current_month_shards() 分片预热
  → broker.start() 启动 5 个 MQ 消费者
  → start_scheduler() 启动 4 个定时任务
```

**首次部署无需任何手工 DDL。**

MQ 消费者全部使用 `AckPolicy.MANUAL`：消费异常不 ack → RabbitMQ 重投；
幂等由游标 / `dispatched` / `dedup_key` / `uq_*` 唯一约束在数据库层兜底。

---

## 八、安装与启动

### 本地（uv）

```bash
cd be-message-service
uv sync
cp app/.env.example app/.env   # 按需填写 MYSQL_MESSAGE_URL / RABBITMQ_URL / MESSAGE_CONFIG
uv run uvicorn app.main:app --host 0.0.0.0 --port 18739
```

### Docker（推荐）

```bash
cd /home/minato/BilibiliExplosion
docker compose up -d be-message-service
```

容器内端口 `18739`，由 `docker-compose.yml` 的 `MESSAGE_SERVICE_PORT` 映射，
依赖 `rabbitmq` + `mysql`，带 `/health` healthcheck。

### 测试

```bash
cd be-message-service
# 本机直连 MySQL（容器映射端口）
MYSQL_MESSAGE_URL='mysql+aiomysql://root:<pwd>@127.0.0.1:10000/BiliMessageDB?charset=utf8mb4' \
  uv run pytest -q
```

| 测试文件 | 覆盖 |
| --- | --- |
| `tests/test_phase_b_smoke.py` | 通知游标去重 / 受众可见性（ALL·CUSTOM·LEVEL·ROLE·VIP）、事件聚合与幂等、私信写扩散·撤回·陌生人过滤、设置闸门与免打扰、活跃度、未读汇总 |
| `tests/test_phase_c_scheduler.py` | 通知投递任务、批量推送（含免打扰跳过保留计数）、死信补偿 |
| `tests/test_phase_e_enums.py` | 枚举值存取往返（VARCHAR 存 value / INTEGER 存 value / 非原生 ENUM） |
| `tests/test_push_channels.py` | 各推送渠道（需真实 token，未配置则 skip） |

---

## 九、HTTP 接口一览

前缀 `/api/v1/message`，统一返回 `StandardResponse { code, data, msg }`。
完整字段说明见运行时自动生成的 OpenAPI：`http://localhost:18739/docs`。

### 系统通知 `/notify`

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/notify/pull` | 拉取增量通知（`cursor` / `limit`），服务端同步推进游标，重复调用不重复 |
| GET | `/notify/list` | 分页查看历史（`page_num` / `page_size` / `only_unread`），不推进游标 |
| GET | `/notify/unread` | 未读数 |
| POST | `/notify/read` | 标记已读（`notify_ids` 为空=全部已读） |
| POST | `/notify/delete` | 删除（仅自己不可见） |
| POST | `/notify/admin/create` | 发布通知（管理员）：`target_type` = all/role/level/vip/custom；`publish_now=false` 存草稿 |
| POST | `/notify/admin/update/{id}` | 修改通知（管理员） |
| POST | `/notify/admin/revoke/{id}` | 撤回通知（管理员），用户侧立即不可见 |
| GET | `/notify/admin/list` | 通知列表（管理员），可按 `status` 筛选 |

### 事件提醒 `/event`

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/event/report` | 上报互动事件（内部服务调用，**不要求登录态**，`mid` 在 body） |
| GET | `/event/aggregate` | 按 `source_type + source_id` 聚合的卡片列表 |
| GET | `/event/list` | 某个聚合分组下的明细 |
| POST | `/event/read` | 已读，支持 id / 类型 / 聚合分组三种粒度 |
| POST | `/event/delete` | 删除 |
| GET | `/event/unread` | `{like, reply, at, total}` |

### 私信 `/dm`

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/dm/send` | 发送（`filtered=true` 表示对方关了陌生人私信；`content_async=false` 表示已同步兜底） |
| GET | `/dm/sessions` | 会话列表，`relation=normal/stranger` 拆分组 |
| POST | `/dm/session/delete` | 删除会话 |
| GET | `/dm/messages` | 聊天记录，`cursor` 为上一页最小 msgkey（字符串） |
| POST | `/dm/delete` | 删除消息（仅自己视角） |
| POST | `/dm/recall` | 撤回（仅发送者、时间窗内，物理清除分片正文） |
| POST | `/dm/ack` | 会话已读，未读清零并抬高已读水位 |
| GET | `/dm/unread` | 私信未读总数 |

### 消息设置 `/setting`

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/setting` | 获取设置，首次访问按「全部开启」自动初始化 |
| POST | `/setting/update` | 部分更新，只写显式传入的字段 |
| GET | `/setting/activity` | 活跃度快照（`is_active` 决定实时/批量） |

### 聚合与推送

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/msg_feed/unread` | 全站未读汇总 `{like, reply, at, notify, dm, total}` |
| POST | `/msg_feed/heartbeat` | 活跃心跳 |
| POST | `/push/push` | 投递推送到 RabbitMQ，异步分发 |
| POST | `/push/test` | 立即发送测试推送（不经队列） |
| POST | `/push/feedback` | 用户反馈，固定回落站长全局配置 |
| GET | `/health` | 204=存活且 broker 连通，503=broker 未连 |

---

## 十、认证

同项目其它微服务：**不做令牌校验**，完全依赖上游 `puppeteer_Bili` 网关注入的 `x-bili-*` 请求头
（网关已清除客户端伪造值并重写为可信登录态）。

| 依赖 | 行为 |
| --- | --- |
| `CurrentUser` | 可匿名，解析不到返回 `None` |
| `RequiredUser` | 未登录 → 401 |
| `AdminUser` | 非 `role=root` → 403 |

---

## 十一、与其它服务的关系

```
be-bilibili-crawler ─┐
                     ├─(RabbitMQ: message_exchange)─▶ be-message-service ─▶ MySQL BiliMessageDB（站内信：通知/事件/私信索引/设置）
RPA-Browser ─────────┘                                       │
                                                             └─ MySQL bili_msg_content_YYYYMM（私信正文分片）
浏览器 ── nginx /api/ ── puppeteer_Bili 网关 ── REST ──────────┘

# 站外提醒（与站内信解耦）：上游通过 /api/v1/message/push → message.push 队列 → 各第三方渠道（PushMe/PushPlus 等）
```

前端 SDK 由 `http://localhost:18739/openapi.json` 经 `@hey-api/vite-plugin` 生成。
