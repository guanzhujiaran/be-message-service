"""[已废弃] 用户注销 MQ 消费处理。

两阶段注销上线后，注销接口（/deactivate、/admin/deactivate）不再投递
``message.user.deactivate``，而是写 MySQL 冷静期记录；到期物理删除由定时任务
``app.tasks.scheduler.deactivate_expire_job`` 驱动（并同步删除 Casdoor）。

本 handler 保留仅为兼容队列中可能残留的历史消息：收到后仍执行一次幂等的本地物理
删除（PptrUser.deactivate），不涉及 Casdoor。待确认线上无残留消息后可连同
publisher.publish_user_deactivate 与消息路由一并清理。

ack 策略（MANUAL）：
- 成功 → ack；
- 失败 → 记录日志后 ack（不 requeue，避免坏消息无限打转）。注销是幂等操作，
  若因瞬时 DB 抖动失败，运维可重投或手动清理。
"""

from faststream.rabbit import RabbitMessage
from loguru import logger

from app.models.schemas import UserDeactivatePayload
from app.services.user.account import PptrUser


async def handle_user_deactivate(
    payload: UserDeactivatePayload, msg: RabbitMessage
) -> None:
    """消费注销消息，执行完整删除流程。"""
    uid = payload.uid
    try:
        await PptrUser(mid=uid).deactivate()
        logger.info(f"[deactivate] 用户 {uid} 已注销")
    except Exception as e:  # noqa: BLE001
        logger.error(f"[deactivate] 用户 {uid} 注销失败（已 ack，不再重投）: {e}")
    # 无论成功与否都 ack：注销幂等，失败不 requeue，避免无限打转
    await msg.ack()


__all__ = ["handle_user_deactivate"]
