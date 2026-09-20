"""浏览统计消费者注册（2.23.0）。

单资源 `/interaction/status/{bizId}` 投递的浏览统计消息在此消费：
由 `handle_interaction_view` 按 `bizType+bizId+mid` 每用户每资源一行去重（跨自然日才 +1）
异步累计浏览数；资源明确不存在时 ack 丢弃（2.63.0 加固），
其余失败按 `x-retry-count` 计数重投（2.63.1：requeue 不写 `x-death`，
计数失效会让脏消息无限打转），不可重试错误直接丢弃。
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
