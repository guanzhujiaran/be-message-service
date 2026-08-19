"""浏览统计 MQ 消费处理（2.23.0）。

`/interaction/status` 接口只投递 `InteractionViewPayload`，真实去重累计在本 handler：

- dynamic 走 `MomentStatService.report_view`（`TMomentViewLog` + `TMomentStat.viewCount`）；
- 非 dynamic 走 `InteractionStatService.report_view`（`TInteractionViewLog` + `TInteractionStat.viewCount`）。

ack 策略（MANUAL）：
- 成功 → ack；
- 失败 → `nack(requeue=True)`，交给 RabbitMQ **独立重试**（浏览上报幂等——
  ViewLog 唯一约束保证 Stat.viewCount 只首次 +1，重复消费无副作用，不会因打转污染计数）。
"""

from faststream.rabbit import RabbitMessage
from loguru import logger

from app.core.database import new_session
from app.models.enums import InteractionBizTypeEnum
from app.models.schemas import InteractionViewPayload
from app.services.interaction import (
    BeMessageInteractionStatService as InteractionStatService,
)
from app.services.moment_stat import MomentStatService


async def handle_interaction_view(payload: InteractionViewPayload, msg: RabbitMessage) -> None:
    """消费浏览统计消息，去重累计浏览数。"""
    try:
        biz_id = int(str(payload.bizId).strip())
    except (TypeError, ValueError):
        logger.error(f"[interaction_view] 非法 bizId={payload.bizId}，ack 丢弃")
        await msg.ack()
        return
    try:
        biz_type = InteractionBizTypeEnum.from_text(payload.bizType)
    except (ValueError, KeyError):
        logger.error(f"[interaction_view] 非法 bizType={payload.bizType}，ack 丢弃")
        await msg.ack()
        return

    async with new_session() as session:
        try:
            if biz_type == InteractionBizTypeEnum.DYNAMIC:
                await MomentStatService.report_view(session, biz_id, payload.mid, payload.refDate)
            else:
                await InteractionStatService.report_view(session, biz_type, biz_id, payload.mid, payload.refDate)
            await session.commit()
        except Exception as e:  # noqa: BLE001
            await session.rollback()
            logger.error(f"[interaction_view] 消费失败 bizType={payload.bizType} bizId={payload.bizId}: {e}，requeue 重试")
            await msg.nack(requeue=True)
            return
    await msg.ack()


__all__ = ["handle_interaction_view"]
