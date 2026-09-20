"""MQ 消费重试计数 / 重投公共工具（2.48.0；2.63.1 修正计数失效）。

RabbitMQ 没有内建重试次数：失败后重投的消息永不消失，一旦消息本身「永远失败」
（脏数据 / 代码 bug）就会打转死循环。统一做法：

- **可重试失败**（DB 抖动 / 连接断开 / 下游超时）→ 重新投递并递增计数；
- **重投次数达上限** → `ack()` 丢弃并记 ERROR，避免死循环污染队列；
- **不可重试失败**（数据截断 / 约束冲突 / schema 错误 / 参数类型错误——
  **消息或数据本身有问题**，重投一万次也不会变好）→ 直接 `ack()` 丢弃并记 ERROR。

⚠️ 2.63.1：`x-death` 头**不能**用来统计 requeue 次数。RabbitMQ 只在消息经
**死信交换机**路由时才注入 `x-death`，`nack(requeue=True)` 重投同一队列不会写入它，
旧实现因此恒返回 0，「最大重试次数」形同虚设——实测同一条消息反复打印「第 1 次失败」。
改为重投时自带 `x-retry-count` 头：计数随消息走，多实例 / 进程重启都不丢；
`x-death` 仍作为真死信场景的回落口径。

用法::

    retries = retry_count(msg)
    if not is_retryable(e):
        await msg.ack()          # 永久错误：丢弃
        return
    if retries + 1 >= MAX_RETRIES:
        await msg.ack()          # 重试耗尽：丢弃
        return
    await republish_with_retry_count(  # 可重试：带计数重投，随后 ack 当前消息
        message=payload.model_dump(mode="json"),
        msg=msg,
        broker=broker,
        exchange=message_exchange,
        queue=interaction_view_queue,
        routing_key=RK_INTERACTION_VIEW,
        retries=retries + 1,
    )
    await msg.ack()
"""

from typing import Any

from faststream.rabbit import RabbitBroker, RabbitExchange, RabbitMessage, RabbitQueue
from loguru import logger
from sqlalchemy.exc import DataError, IntegrityError, ProgrammingError

#: 自定义重投计数消息头（随消息走，不依赖 x-death）
RETRY_COUNT_HEADER = "x-retry-count"

#: 默认最大重投次数（各 handler 可按业务覆盖；含首次消费在内的总尝试次数）
DEFAULT_MAX_RETRIES = 3

#: 不可重试异常：**消息 / 数据本身有问题**，重投不会成功，直接丢弃并告警排查。
#: - DataError：数据截断 / 类型不匹配（如原生 ENUM 缺成员 → 1265）
#: - IntegrityError：唯一约束 / 外键冲突
#: - ProgrammingError：表 / 列不存在等 schema 错误
#: - ValueError / TypeError / KeyError / AttributeError：脏消息 / 代码 bug
_NON_RETRYABLE_EXC: tuple[type[BaseException], ...] = (
    DataError,
    IntegrityError,
    ProgrammingError,
    ValueError,
    TypeError,
    KeyError,
    AttributeError,
)


def is_retryable(exc: BaseException) -> bool:
    """判断异常是否值得重投（False = 消息本身有问题，应直接 ack 丢弃）。"""
    return not isinstance(exc, _NON_RETRYABLE_EXC)


def _to_int(value: Any) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0


def message_headers(msg: RabbitMessage) -> dict[str, Any]:
    """读取消息头（拿不到时返回空 dict，不因头缺失中断消费流程）。"""
    try:
        return dict(msg.raw_message.headers or {})
    except Exception:  # noqa: BLE001
        return {}


def retry_count(msg: RabbitMessage) -> int:
    """统计该消息已被重投的次数（优先 `x-retry-count`，回落 `x-death` 兼容真死信）。"""
    try:
        headers = message_headers(msg)
        counts = [_to_int(headers.get(RETRY_COUNT_HEADER))]
        x_death = headers.get("x-death") or []
        counts += [_to_int(d.get("count", 0)) for d in x_death if isinstance(d, dict)]
        return max(counts)
    except Exception:  # noqa: BLE001
        return 0


async def republish_with_retry_count(
    message: Any,
    msg: RabbitMessage,
    *,
    broker: RabbitBroker,
    exchange: RabbitExchange,
    queue: RabbitQueue,
    routing_key: str,
    retries: int,
) -> bool:
    """重新投递消息并写入递增后的重投计数（调用方随后 `ack()` 当前消息）。

    不走 `nack(requeue=True)`：requeue 不写 `x-death`，无法计数，会无限打转。

    Returns:
        True=重投成功（调用方 ack 当前消息）；False=重投失败（调用方同样 ack 丢弃，
        避免回到无计数的 requeue 死循环，并记 ERROR 便于排查）。
    """
    headers = {k: v for k, v in message_headers(msg).items() if k != RETRY_COUNT_HEADER}
    headers[RETRY_COUNT_HEADER] = retries
    try:
        await broker.publish(
            message=message,
            exchange=exchange,
            routing_key=routing_key,
            queue=queue,
            headers=headers,
        )
        return True
    except Exception as e:  # noqa: BLE001
        logger.error(f"MQ 重投失败 routing_key={routing_key} retries={retries}: {e}")
        return False


__all__ = [
    "DEFAULT_MAX_RETRIES",
    "RETRY_COUNT_HEADER",
    "is_retryable",
    "message_headers",
    "republish_with_retry_count",
    "retry_count",
]
