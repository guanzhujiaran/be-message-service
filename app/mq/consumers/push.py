"""外部渠道推送消费者。

把投递到 `message_queue`（routing_key=`message.push`）的推送请求消费掉，
分发到 PushMe / PushPlus 等第三方渠道。

推送失败由 handler 直接抛错；subscriber 用 NACK_ON_ERROR 让失败消息自动重回
队列重试，无需 try/except 静默吞错。
"""

from faststream import AckPolicy
from faststream.rabbit.fastapi import RabbitMessage

from app.consumers.push import handle_message
from app.core.broker import message_exchange, message_queue
from app.models import PushMessagePayload
from app.mq.router import router


@router.subscriber(
    queue=message_queue,
    exchange=message_exchange,
    ack_policy=AckPolicy.NACK_ON_ERROR,
)
async def consume_message(message: PushMessagePayload, msg: RabbitMessage) -> None:
    """外部渠道推送。"""
    await handle_message(message, msg)
