"""外部渠道推送消费者。

把投递到 `message_queue`（routing_key=`message.push`）的推送请求消费掉，
分发到 PushMe / PushPlus 等第三方渠道。

推送失败**不重投**：handler 内部捕获异常并记日志后正常返回，subscriber 用
`AckPolicy.ACK` 保证消费即 ack（失败也 ack）——消息直接丢弃，不会反复推送
直至死信。站外提醒属尽力而为，失败不补。
"""

from faststream import AckPolicy
from faststream.rabbit.fastapi import RabbitMessage

from app.consumers.external_push import handle_message
from app.core.broker import message_exchange, message_queue
from app.models import PushMessagePayload
from app.mq.router import router


@router.subscriber(
    queue=message_queue,
    exchange=message_exchange,
    ack_policy=AckPolicy.ACK,
)
async def consume_message(message: PushMessagePayload, msg: RabbitMessage) -> None:
    """外部渠道推送（失败即丢弃，不重投）。"""
    await handle_message(message, msg)
