"""浏览统计 MQ 消费处理（2.23.0；2.42.0 每用户每资源一行）。

`/interaction/status` 接口只投递 `InteractionViewPayload`，真实去重累计在本 handler：

- dynamic 走 `MomentStatService.report_view`（`TInteractionViewLog` + `TInteractionStat.viewCount`）；
- 非 dynamic 走 `InteractionStatService.report_view`（`TInteractionViewLog` + `TInteractionStat.viewCount`）。
- 2.42.0：明细表每用户每资源一行（唯一约束 bizType+bizId+mid），
  `lastViewAt` 与当前时间是否同一自然日判断跨天访问才给 Stat +1。

ack 策略（MANUAL）：
- 成功 → ack；
- **资源明确不存在 → ack 丢弃**（2.63.0 加固）：投递端（单资源 `GET /interaction/status/{bizId}`）
  已在投递前校验资源存在，这里做二次兜底，防止有人绕过投递端直接往队列塞任意 `bizId`，
  从而在 `TInteractionViewLog`（每用户每资源一行）与 `TInteractionStat` 里造脏行；
  判定用 **三态存在性** `check_exists_state()`：`False` 明确不存在 → 丢弃，`None` 校验不可用
  （归属服务 RPC 失败）→ 按弱依赖继续计数，避免下游抖动静默吞掉真实浏览；
- 失败 → 重投（带 `x-retry-count` 计数，2.63.1：不再用 `nack(requeue=True)`，
  requeue 不写 `x-death` 导致「最大重试次数」失效、脏消息无限打转）；
  **不可重试失败**（数据截断 / 约束冲突 / 脏消息等永久错误）直接 ack 丢弃；
- **最大重试次数**：重投计数达 `MAX_VIEW_RETRIES` → 记 ERROR 日志并
  `ack()` 丢弃，避免消息打转死循环、不污染计数；
- 服务关停（SIGINT）抛出的 `asyncio.CancelledError`：不 rollback / 不 nack，直接上抛交给框架处理，
  避免关停期间产生二次异常噪音（此前版本在 except 分支内 rollback 会再触发 CancelledError）。
"""

import asyncio

from bili_common.models import InteractionBizTypeEnum
from faststream.rabbit import RabbitMessage
from loguru import logger

from app.consumers.retry import (
    DEFAULT_MAX_RETRIES,
    is_retryable,
    republish_with_retry_count,
    retry_count,
)
from app.core.broker import (
    RK_INTERACTION_VIEW,
    broker,
    interaction_view_queue,
    message_exchange,
)
from app.core.database import new_session
from app.models.schemas import InteractionViewPayload
from app.services.interaction_actions import get_biz

#: 浏览统计最大重投次数（含首次消费在内的总尝试次数，超过则 ack 丢弃）
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
        except ValueError:
            # 资源类型未登记（脏消息 / 版本不匹配）：不可能被正确计量，ack 丢弃
            logger.error(
                f"[interaction_view] 资源类型未登记 bizType={payload.bizType}，ack 丢弃"
            )
            await msg.ack()
            return
        try:
            # 2.63.0 加固：二次兜底校验资源存在性（投递端已校验，此处防绕过投递端的脏消息）
            exists = await biz.check_exists_state()
            if exists is False:
                logger.warning(
                    f"[interaction_view] 资源不存在，ack 丢弃（不写浏览明细 / 计数） "
                    f"bizType={payload.bizType} bizId={payload.bizId} mid={payload.mid}"
                )
                await msg.ack()
                return
            if exists is None:
                # 弱依赖：校验不可用（下游 RPC 失败）不当作「不存在」，照旧计数
                logger.warning(
                    f"[interaction_view] 存在性校验不可用，按弱依赖继续计数 "
                    f"bizType={payload.bizType} bizId={payload.bizId}"
                )
            await biz.view()
            await session.commit()
        except asyncio.CancelledError:
            # 服务关停：不 rollback / 不 nack，上抛交由框架处理
            raise
        except Exception as e:  # noqa: BLE001
            await session.rollback()
            if not is_retryable(e):
                # 永久错误（脏消息 / 数据截断 / 约束冲突）：重投也不会成功，直接丢弃并告警
                logger.error(
                    f"[interaction_view] 消费失败（不可重试，ack 丢弃） "
                    f"bizType={payload.bizType} bizId={payload.bizId}: {e}"
                )
                await msg.ack()
                return
            retries = retry_count(msg) + 1
            if retries >= MAX_VIEW_RETRIES:
                logger.error(
                    f"[interaction_view] 消费失败已达最大重试 {MAX_VIEW_RETRIES} 次，ack 丢弃 "
                    f"bizType={payload.bizType} bizId={payload.bizId}: {e}"
                )
                await msg.ack()
                return
            logger.error(
                f"[interaction_view] 消费失败 bizType={payload.bizType} bizId={payload.bizId} "
                f"(第 {retries} 次失败)，重投重试: {e}"
            )
            await republish_with_retry_count(
                message=payload.model_dump(mode="json"),
                msg=msg,
                broker=broker,
                exchange=message_exchange,
                queue=interaction_view_queue,
                routing_key=RK_INTERACTION_VIEW,
                retries=retries,
            )
            await msg.ack()
            return
    await msg.ack()


__all__ = ["MAX_VIEW_RETRIES", "handle_interaction_view"]
