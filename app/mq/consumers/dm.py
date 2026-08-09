"""私信正文异步落库消费者。

私信内容写入是写路径的关键链路，独立队列保证不被推送链路拖慢。
handler 使用 MANUAL ack 策略：成功写入分片后 ack，失败则转死信表后 ack
（不 requeue，避免坏消息无限打转）。
"""

from faststream import AckPolicy
from faststream.rabbit.fastapi import RabbitMessage

from app.consumers.dm import handle_dm_content
from app.core.broker import dm_content_queue, message_exchange
from app.models.schemas import DmContentPayload
from app.mq.router import router


@router.subscriber(
    queue=dm_content_queue,
    exchange=message_exchange,
    ack_policy=AckPolicy.MANUAL,
)
async def consume_dm_content(message: DmContentPayload, msg: RabbitMessage) -> None:
    """私信正文异步落库（月度分库 + 库内 100 表）。"""
    await handle_dm_content(message, msg)
