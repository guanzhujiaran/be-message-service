"""用户注销 MQ 消费者注册。

注销接口投递 `message.user.deactivate` 消息，本消费者异步执行完整删除流程。
handler 使用 MANUAL ack：成功 ack；失败记录日志后 ack（不 requeue，注销幂等，
避免坏消息无限打转）。
"""

from faststream import AckPolicy
from faststream.rabbit.fastapi import RabbitMessage

from app.consumers.deactivate import handle_user_deactivate
from app.core.broker import message_exchange, user_deactivate_queue
from app.models.schemas import UserDeactivatePayload
from app.mq.router import router


@router.subscriber(
    queue=user_deactivate_queue,
    exchange=message_exchange,
    ack_policy=AckPolicy.MANUAL,
)
async def consume_user_deactivate(
    message: UserDeactivatePayload, msg: RabbitMessage
) -> None:
    """异步执行用户注销（pptr 四表物理删除 + be-message 业务数据清除）。"""
    await handle_user_deactivate(message, msg)
