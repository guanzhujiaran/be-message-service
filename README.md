# MessageService — 统一消息系统微服务

基于 **RabbitMQ + FastStream** 的统一消息系统微服务，集中接管 `FastapiApp` 与 `RPA-Browser`
的消息能力。**「推送（push）」是当前已实现的首个模块**，后续评论（comment）/ 对话（conversation）/
私信（dm）等模块将复用同一套 broker / 队列基础设施，按 routing_key 区分（如 `message.push`、
`message.comment`、...）。

当前推送模块将告警/通知统一投递到 **PushMe / PushPlus / 邮箱(SMTP)** 等渠道。

## 架构

```
FastapiApp.a_pushme ─┐
                     ├─(RabbitMQ: message_exchange / message_queue)─▶ MessageService ─▶ PushMe / PushPlus / 邮箱 / ...
RPA-Browser.push_msg ┘
```

- **生产者**：`FastapiApp`（全局告警）与 `RPA-Browser`（per-user 配置）只负责把
  `PushMessage{title, content, push_type, config, user}` 发布到 RabbitMQ，**不再直接调用推送接口**。
- **消费者**：`MessageService` 消费消息，根据配置（消息内 per-user 配置优先，否则回落到本服务
  环境变量中的全局渠道）调用对应渠道发送。

后续模块（评论 / 对话 / 私信）将复用同一 `message_exchange`，通过不同的 routing_key 接入各自消费者。

## 消息格式（推送模块）

```json
{
  "title": "标题",
  "content": "正文",
  "push_type": "text",          // 可选，pushme/pushplus 模板类型
  "config": { ... },            // 可选，PushChannelConfig；为空则使用全局环境变量配置
  "user": { ... },              // 可选，推送发起方用户信息（由 pptr 经 x-bili-* 头透传）
  "requires_login": false       // 可选，为 true 且未登录时拒绝推送并返回需登录提示
}
```

## 本地开发（uv）

```bash
uv sync
# 复制 .env.example 为 .env 并按需填写
# 本服务是标准 FastAPI 应用，用 uvicorn 启动即可
uv run uvicorn app.main:app --host 0.0.0.0 --port 18739
```

## 渠道配置

两种来源，**消息内 `config` 优先，全局环境变量兜底**：

- **全局配置（单一 JSON 环境变量 `MESSAGE_CONFIG`）**：在 `docker-compose.yml` 中与
  `fastapi`、`rpa-browser`、`message-service` **共用同一个变量**，只维护一份。
  字段即 `PushChannelConfig` 的全部字段（`pushme_key` / `push_plus_token` / `smtp_*` / `tg_*` 等），
  只填需要启用的渠道即可。pydantic-settings 会自动把 JSON 字符串解析为模型。
- **per-user 配置**：RPA-Browser 数据库保存的 `PushChannelConfig`，发布消息时作为 `config` 字段
  一并发送，优先级高于全局配置，因此支持 Bark / 钉钉 / 飞书 / Server酱 / 企业微信 / Ntfy /
  WxPusher 等全部渠道。

`MESSAGE_CONFIG` 在 `.env` 中用单引号包裹整段 JSON，例如：

```bash
MESSAGE_CONFIG='{"pushme_key":"Uxxx","push_plus_token":"yyy","smtp_server":"smtp.x","smtp_ssl":"true","smtp_email":"a@b.c","smtp_password":"pw","smtp_name":"告警"}'
```

RPA-Browser 若某用户没有配置通知，则会回落到该全局 `MESSAGE_CONFIG`；FastapiApp 与
`message-service` 也读取同一变量，避免多份变量不一致。

## 健康检查

除消费 RabbitMQ 外，服务以 **FastAPI 应用 + FastStream 作为 FastAPI 插件（lifespan 接入）**
的方式，额外暴露标准 REST 接口与健康检查路由：

```
GET http://message-service:18739/health
```

访问返回 2xx 即代表服务存活且 RabbitMQ broker 连接正常。`FastapiApp` 在启动（lifespan）
时会访问该路由以判断 `message-service` 是否连通；`docker-compose.yml` 也据此配置了容器
`healthcheck`。本地运行时会在 `--host`/`--port`（默认 `0.0.0.0:18739`）上监听。

## HTTP 接口（FastStream FastAPI 插件）

除 RabbitMQ 消费者外，本服务以 **FastAPI 应用** 作为 ASGI 主体，并通过 `lifespan` 把
FastStream 的 RabbitMQ broker 作为「FastAPI 插件」接入（启动即连接并开始消费，关闭即断开），
在此基础上额外暴露标准 REST 接口，供 `FastapiApp` / `RPA-Browser` / 前端（经
`nodejs-pptr` 转发）直接调用，**替代原先「直接调用 PushMe / PushPlus 第三方接口」的方式**。

接口命名、返回结构、认证方式均与项目内其它微服务（如 `RPA-Browser`）保持一致：

- 前缀：`/api/v1/message`（推送模块挂载于 `/api/v1/message/push`）
- 统一返回：`{ "code": 0, "data": ..., "msg": "success" }`（`code != 0` 表示失败）

### 接口列表（推送模块）

| 方法 | 路径 | 说明 |
| ---- | ---- | ---- |
| POST | `/api/v1/message/push` | 投递一条推送请求到 RabbitMQ，由消费者异步分发（等价于各微服务原先「直接调 PushMe/PushPlus」的逻辑） |
| POST | `/api/v1/message/push/test` | 立即发送一条测试推送（不经过队列），用于验证渠道配置是否生效 |

### 请求示例

`/api/v1/message/push` 的请求体与 MQ 消息结构一致：

```json
{
  "title": "标题",
  "content": "正文",
  "push_type": "text",
  "config": { "pushme_key": "Uxxx", "push_plus_token": "yyy" }
}
```

`config` 可省略，省略时使用本服务的全局环境变量 `MESSAGE_CONFIG` 作为渠道配置。

### 前端接入

前端在 `vite.config.ts` 中通过 `@hey-api/vite-plugin` 的 `heyApiPlugin` 以
`http://localhost:18739/openapi.json` 生成 SDK；运行时经 `nodejs-pptr` 的 `createProxyMiddleware`
（`/api/v1/message` → message-service）转发。

## Docker

已在根目录 `docker-compose.yml` 中注册为 `message-service` 服务，随 `docker-compose up -d`
一同启动，依赖 `rabbitmq`。
