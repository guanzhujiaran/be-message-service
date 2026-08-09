"""FastStream 消费者：评论相关的后台作业（Phase 3.6，reply-job）。

三个队列与发布主流程解耦，作为**备用投递通道 + 定时补偿入口**：

- `message.comment.notify`：回复 / 点赞 / @ 的事件提醒。当前评论发布已在主流程内
  同步调用 `EventService.report`（同进程，见 app.services.comment），此消费者作为
  解耦后的冗余通道，依赖 `dedup_key` 幂等保证重复投递不刷屏。
- `message.comment.audit`：异步内容复审（AI 复审 / 超时重试），弱依赖。
- `message.comment.count`：楼层发号与计数削峰、写倾斜补偿。

所有 handler 都做防御式 ack/nack，且当前没有生产者主动投递这些队列，
因此运行时保持空闲，不会与同步路径产生重复副作用。
"""

import traceback

from faststream.rabbit import RabbitMessage
from loguru import logger


async def handle_comment_notify(payload: dict, msg: RabbitMessage) -> None:
    """评论互动通知的备用投递通道。

    当前评论发布已在主流程内同步调用 `EventService.report`
    （见 `app.services.comment` 的 `_notify_reply` / `_notify_at`），本消费者作为
    解耦后的冗余通道：真实生产者在 Phase 3 尚未接入，这里仅做幂等校验占位，
    依赖 `dedup_key` 保证即便未来双投也不会刷屏。
    """
    try:
        from app.models.schemas import EventReportReq

        EventReportReq.model_validate(payload)
        logger.debug("评论通知备用通道收到合法载荷（当前无生产者投递）")
        await msg.ack()
    except Exception as e:  # noqa: BLE001
        logger.error(f"评论通知消费失败: {e}\n{traceback.format_exc()}")
        await msg.nack(requeue=False)


async def handle_comment_audit(payload: dict, msg: RabbitMessage) -> None:
    """异步内容复审（弱依赖，失败不阻塞）。"""
    try:
        rpid = int(str(payload.get("rpid", "")).strip())
        logger.debug(f"评论异步复审任务 rpid={rpid}（当前无生产者投递，备用通道）")
        await msg.ack()
    except Exception as e:  # noqa: BLE001
        logger.error(f"评论复审消费失败: {e}\n{traceback.format_exc()}")
        await msg.nack(requeue=False)


async def handle_comment_count(payload: dict, msg: RabbitMessage) -> None:
    """楼层发号 / 计数削峰与补偿（弱依赖）。"""
    try:
        oid = payload.get("oid")
        logger.debug(f"评论计数任务 oid={oid}（当前无生产者投递，备用通道）")
        await msg.ack()
    except Exception as e:  # noqa: BLE001
        logger.error(f"评论计数消费失败: {e}\n{traceback.format_exc()}")
        await msg.nack(requeue=False)


__all__ = [
    "handle_comment_notify",
    "handle_comment_audit",
    "handle_comment_count",
]
