"""评论相关后台作业消费者（Phase 3.6 备用通道）。

三类消费者均使用 MANUAL ack 策略，handler 内做防御式 ack/nack：

- `message.comment.notify`：回复 / 点赞 / @ 的事件提醒备用通道
- `message.comment.audit`：异步内容复审（弱依赖）
- `message.comment.count`：楼层发号 / 计数削峰与补偿

当前评论的审核 / 通知已在发布主流程内同步完成（同进程调用 EventService），
这些队列作为解耦后的备用投递通道与定时补偿入口，按 Phase 3.6 预留。
"""

from faststream import AckPolicy
from faststream.rabbit.fastapi import RabbitMessage

from app.consumers.comment import (
    handle_comment_audit,
    handle_comment_count,
    handle_comment_notify,
)
from app.core.broker import (
    comment_audit_queue,
    comment_count_queue,
    comment_notify_queue,
    message_exchange,
)
from app.mq.router import router


@router.subscriber(
    queue=comment_notify_queue,
    exchange=message_exchange,
    ack_policy=AckPolicy.MANUAL,
)
async def consume_comment_notify(message: dict, msg: RabbitMessage) -> None:
    """评论互动通知的备用投递通道。"""
    await handle_comment_notify(message, msg)


@router.subscriber(
    queue=comment_audit_queue,
    exchange=message_exchange,
    ack_policy=AckPolicy.MANUAL,
)
async def consume_comment_audit(message: dict, msg: RabbitMessage) -> None:
    """评论异步复审（弱依赖）。"""
    await handle_comment_audit(message, msg)


@router.subscriber(
    queue=comment_count_queue,
    exchange=message_exchange,
    ack_policy=AckPolicy.MANUAL,
)
async def consume_comment_count(message: dict, msg: RabbitMessage) -> None:
    """评论楼层发号 / 计数削峰。"""
    await handle_comment_count(message, msg)
