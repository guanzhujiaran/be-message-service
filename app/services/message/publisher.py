"""统一的 MQ 投递封装。

把「构造 payload → 选 exchange / routing_key → publish → 异常兜底」收敛到一处，
业务服务只关心「发什么」，不关心「怎么发」。

所有投递都做了异常吞掉处理并返回 bool：MQ 抖动不应该让主流程（发私信、
上报事件）失败，调用方拿到 False 后各自决定降级策略
（如私信内容改为同步落库、事件提醒记为未推送等待补偿）。

注意：**站内信（系统通知 / 事件提醒 / 私信）的投递一律走数据库写路径，
不在这里向第三方推送渠道发消息**。第三方推送（PushMe / PushPlus 等）只用于
「站外提醒」类内容，由 `/api/v1/message/push` 经 `publish_channel_push` 投递。
"""

from loguru import logger

from app.core.broker import (
    RK_DM_CONTENT,
    RK_INTERACTION_VIEW,
    RK_PUSH,
    RK_USER_DEACTIVATE,
    broker,
    dm_content_queue,
    interaction_view_queue,
    message_exchange,
    message_queue,
    user_deactivate_queue,
)
from app.models.push import PushMessagePayload
from app.models.schemas import DmContentPayload, InteractionViewPayload, UserDeactivatePayload


async def _publish(payload, routing_key: str, queue) -> bool:
    try:
        await broker.publish(
            message=payload.model_dump(mode="json"),
            exchange=message_exchange,
            routing_key=routing_key,
            queue=queue,
        )
        return True
    except Exception as e:  # noqa: BLE001
        logger.error(f"MQ 投递失败 routing_key={routing_key}: {e}")
        return False


async def publish_dm_content(payload: DmContentPayload) -> bool:
    """投递私信正文，由消费者异步写入月度分库的分表。"""
    return await _publish(payload, RK_DM_CONTENT, dm_content_queue)


async def publish_channel_push(title: str, content: str) -> bool:
    """投递到外部渠道推送队列（仅用于站外提醒类内容，与站内信无关）。"""
    return await _publish(
        PushMessagePayload(title=title, content=content, push_type="text", config=None),
        RK_PUSH,
        message_queue,
    )


async def publish_user_deactivate(uid: int) -> bool:
    """投递用户注销消息，由消费者异步执行完整删除流程。

    Args:
        uid: 待注销用户 mid（>0，调用方已校验）。

    Returns:
        bool: 投递是否成功（失败时调用方按需降级处理）。
    """
    return await _publish(
        UserDeactivatePayload(uid=uid),
        RK_USER_DEACTIVATE,
        user_deactivate_queue,
    )


async def publish_interaction_view(payload: InteractionViewPayload) -> bool:
    """投递浏览统计消息，由消费者异步去重累计（2.23.0）。

    投递失败返回 False（MQ 抖动不阻塞 status 主链路）；浏览上报幂等，
    消费者经 ViewLog 唯一约束保证 Stat.viewCount 只首次 +1。
    """
    return await _publish(payload, RK_INTERACTION_VIEW, interaction_view_queue)


__all__ = [
    "publish_channel_push",
    "publish_dm_content",
    "publish_interaction_view",
    "publish_user_deactivate",
]
