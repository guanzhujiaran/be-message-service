"""消息系统共用的 RabbitMQ broker / exchange / queue 定义。

同时被消费者（main.py 的 subscriber）与 HTTP 接口（app.api.push）复用，
避免重复定义导致绑定不一致。后续评论 / 对话 / 私信等模块可复用同一 exchange，
按 routing_key（message.push / message.comment / ...）区分。
"""

from faststream.rabbit import RabbitBroker, RabbitExchange, RabbitQueue, ExchangeType

from app.config import settings

broker = RabbitBroker(settings.rabbitmq_url)

message_exchange = RabbitExchange(
    "message_exchange",
    type=ExchangeType.TOPIC,
    durable=True,
    auto_delete=False,
)
message_queue = RabbitQueue(
    "message_queue",
    routing_key="message.#",
    durable=True,
)
