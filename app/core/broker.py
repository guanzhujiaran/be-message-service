"""消息系统共用的 RabbitMQ broker / exchange / queue 定义。

同时被消费者（app.consumers）与 HTTP 接口（app.api.*）复用，
避免重复定义导致绑定不一致。

所有模块共用一个 TOPIC exchange `message_exchange`，按 routing_key 分流到独立队列：

| routing_key            | queue                    | 用途                        |
| ---------------------- | ------------------------ | --------------------------- |
| `message.push`         | `message_queue`          | 外部渠道推送（独立于站内信）|
| `message.dm.content`   | `message_dm_content_queue` | 私信内容异步落库（分库分表） |

模块说明：
- **站内信（系统通知 / 事件提醒 / 私信）**：写路径直接落库（读扩散 / 写扩散），
  用户通过 `/pull` `/list` `/messages` `msg_feed` 等接口主动拉取，
  **不经由任何第三方推送渠道**。把站内信和第三方推送混在一起是早期设计错误，
  现已拆分：第三方推送（PushMe / PushPlus 等）仅用于「站外提醒」类内容，
  由 `/api/v1/message/push` 投递到 `message.push` 队列单独处理。
- **私信内容落库**是写路径的关键链路，必须独立队列，避免与任何推送链路耦合。

历史 `message_queue` 的 routing_key 保持 `message.push`（原为 `message.#`），
否则它会把新增的 dm 等消息一并吃掉。
"""

from faststream.rabbit import ExchangeType, RabbitExchange, RabbitQueue

# broker 实例由 RabbitRouter 统一创建并管理生命周期（start/stop 都在
# router 的 lifespan 内完成）。这里复用同一个实例，保证 publisher / RPC /
# 健康检查与消费者共用一条连接，避免起多个 RabbitMQ 连接。
# 注意：必须 lazy import 以避免与 app.mq.router 之间产生循环导入——
# router.py 只依赖 app.core.config / app.tasks，不依赖本模块。
from app.mq.router import broker

message_exchange = RabbitExchange(
    "message_exchange",
    type=ExchangeType.TOPIC,
    durable=True,
    auto_delete=False,
)

# ==================== routing key 常量 ====================
# 外部渠道推送（站外提醒，与站内信无关，仅由 /api/v1/message/push 投递）
RK_PUSH = "message.push"
# 私信内容异步落库
RK_DM_CONTENT = "message.dm.content"
# 评论：回复 / 点赞 / @ → 事件提醒（弱依赖，与发布主流程解耦的备用投递通道）
RK_COMMENT_NOTIFY = "message.comment.notify"
# 评论：异步内容审核（AI 复审 / 定时重试）
RK_COMMENT_AUDIT = "message.comment.audit"
# 评论：楼层发号与计数削峰（串行化，防写倾斜）
RK_COMMENT_COUNT = "message.comment.count"

# ==================== 队列 ====================
# 外部渠道推送（站外提醒，独立于站内信）
message_queue = RabbitQueue(
    "message_queue",
    routing_key=RK_PUSH,
    durable=True,
)

# 私信内容异步落库：写路径核心，独立队列保证不被推送链路拖慢
dm_content_queue = RabbitQueue(
    "message_dm_content_queue",
    routing_key=RK_DM_CONTENT,
    durable=True,
)

# 评论三类备用消费者队列（与站内信 / 私信物理隔离）。
# 当前评论的审核 / 通知已在发布主流程内同步完成（同进程调用 EventService），
# 这些队列作为解耦后的备用投递通道与定时补偿入口，按 Phase 3.6 预留。
comment_notify_queue = RabbitQueue(
    "message_comment_notify_queue",
    routing_key=RK_COMMENT_NOTIFY,
    durable=True,
)
comment_audit_queue = RabbitQueue(
    "message_comment_audit_queue",
    routing_key=RK_COMMENT_AUDIT,
    durable=True,
)
comment_count_queue = RabbitQueue(
    "message_comment_count_queue",
    routing_key=RK_COMMENT_COUNT,
    durable=True,
)


__all__ = [
    "broker",
    "message_exchange",
    "message_queue",
    "dm_content_queue",
    "comment_notify_queue",
    "comment_audit_queue",
    "comment_count_queue",
    "RK_PUSH",
    "RK_DM_CONTENT",
    "RK_COMMENT_NOTIFY",
    "RK_COMMENT_AUDIT",
    "RK_COMMENT_COUNT",
]
