"""浏览统计消费者注册（2.23.0）。

`/interaction/status` 投递的浏览统计消息在此消费：
由 `handle_interaction_view` 按 `bizType+bizId+mid+refDate` 去重异步累计浏览数，
消费失败 `nack(requeue=True)` 由 RabbitMQ 独立重试（浏览上报幂等，重复消费无副作用）。
"""

from faststream import AckPolicy
from faststream.rabbit.fastapi import RabbitMessage

from app.consumers.interaction_view import handle_interaction_view
from app.core.broker import interaction_view_queue, message_exchange
from app.models.schemas import InteractionViewPayload
from app.mq.router import router


@router.subscriber(
    queue=interaction_view_queue,
    exchange=message_exchange,
    ack_policy=AckPolicy.MANUAL,
)
async def consume_interaction_view(message: InteractionViewPayload, msg: RabbitMessage) -> None:
    """消费浏览统计消息，去重累计浏览数。"""
    await handle_interaction_view(message, msg)
