"""浏览统计 MQ 消费处理（2.23.0；2.42.0 每用户每资源一行）。

`/interaction/status` 接口只投递 `InteractionViewPayload`，真实去重累计在本 handler：

- dynamic 走 `MomentStatService.report_view`（`TInteractionViewLog` + `TInteractionStat.viewCount`）；
- 非 dynamic 走 `InteractionStatService.report_view`（`TInteractionViewLog` + `TInteractionStat.viewCount`）。
- 2.42.0：明细表每用户每资源一行（唯一约束 bizType+bizId+mid），
  `lastViewAt` 与当前时间是否同一自然日判断跨天访问才给 Stat +1。

ack 策略（MANUAL）：
- 成功 → ack；
- 失败 → `nack(requeue=True)` 交给 RabbitMQ 重试（浏览上报幂等——
  ViewLog 唯一约束保证 Stat.viewCount 只首次 +1，重复消费无副作用）；
- **最大重试次数**：重投超过 `MAX_VIEW_RETRIES` 次（经 RabbitMQ `x-death` 消息头统计）→ 记 ERROR 日志并
  `ack()` 丢弃，避免消息打转死循环、不污染计数；
- 服务关停（SIGINT）抛出的 `asyncio.CancelledError`：不 rollback / 不 nack，直接上抛交给框架处理，
  避免关停期间产生二次异常噪音（此前版本在 except 分支内 rollback 会再触发 CancelledError）。
"""

import asyncio

from faststream.rabbit import RabbitMessage
from loguru import logger

from app.consumers.retry import DEFAULT_MAX_RETRIES, retry_count
from app.core.database import new_session
from bili_common.models import InteractionBizTypeEnum
from app.models.schemas import InteractionViewPayload
from app.services.interaction_actions import get_biz

#: 浏览统计最大重试次数（requeue 重投超过该次数则 ack 丢弃）
MAX_VIEW_RETRIES = DEFAULT_MAX_RETRIES


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
            # 2.48.0：以资源为主体，直接取资源实例调用 view()
            biz = get_biz(biz_type, session, biz_id, payload.mid)
            await biz.view()
            await session.commit()
        except asyncio.CancelledError:
            # 服务关停：不 rollback / 不 nack，上抛交由框架处理
            raise
        except Exception as e:  # noqa: BLE001
            await session.rollback()
            retries = retry_count(msg)
            if retries >= MAX_VIEW_RETRIES:
                logger.error(
                    f"[interaction_view] 消费失败已达最大重试 {MAX_VIEW_RETRIES} 次，ack 丢弃 "
                    f"bizType={payload.bizType} bizId={payload.bizId}: {e}"
                )
                await msg.ack()
                return
            logger.error(
                f"[interaction_view] 消费失败 bizType={payload.bizType} bizId={payload.bizId} "
                f"(第 {retries + 1} 次失败)，requeue 重试: {e}"
            )
            await msg.nack(requeue=True)
            return
    await msg.ack()


__all__ = ["MAX_VIEW_RETRIES", "handle_interaction_view"]
