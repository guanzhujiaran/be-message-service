"""用户注销 MQ 消费处理。

注销接口只投递消息，真实删除由本 handler 异步执行：
调用 `UserDeactivateService.deactivate(uid)` 物理删除 pptr 四表 + 彻底清除
be-message 业务数据。

ack 策略（MANUAL）：
- 成功 → ack；
- 失败 → 记录日志后 ack（不 requeue，避免坏消息无限打转）。注销是幂等操作，
  若因瞬时 DB 抖动失败，运维可重投或手动清理。
"""

from faststream.rabbit import RabbitMessage
from loguru import logger

from app.models.schemas import UserDeactivatePayload
from app.services.user_deactivate import UserDeactivateService


async def handle_user_deactivate(payload: UserDeactivatePayload, msg: RabbitMessage) -> None:
    """消费注销消息，执行完整删除流程。"""
    uid = payload.uid
    try:
        await UserDeactivateService.deactivate(uid)
        logger.info(f"[deactivate] 用户 {uid} 已注销")
    except Exception as e:  # noqa: BLE001
        logger.error(f"[deactivate] 用户 {uid} 注销失败（已 ack，不再重投）: {e}")
    # 无论成功与否都 ack：注销幂等，失败不 requeue，避免无限打转
    await msg.ack()


__all__ = ["handle_user_deactivate"]
