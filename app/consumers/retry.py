"""MQ 消费重试计数公共工具（2.48.0）。

RabbitMQ 没有内建重试次数：失败后 `nack(requeue=True)` 的消息会被无限重投，
一旦消息本身「永远失败」（脏数据 / 代码 bug）就会打转死循环。统一做法：

- 失败 → `nack(requeue=True)`，让 MQ 重投（DB 抖动可自愈）；
- 重投次数达上限 → `ack()` 丢弃并记 ERROR，避免死循环污染队列。

重投次数从消息头 `x-death` 读取（RabbitMQ 规范：requeue 后注入该数组，
每项含 `count`），各消费者共用本实现，避免重复造轮子。
"""

from faststream.rabbit import RabbitMessage

#: 默认最大重投次数（各 handler 可按业务覆盖）
DEFAULT_MAX_RETRIES = 3


def retry_count(msg: RabbitMessage) -> int:
    """统计该消息已被 requeue 重投的次数（无 `x-death` 头即 0）。"""
    try:
        headers = msg.raw_message.headers or {}
        x_death = headers.get("x-death") or []
        counts = [int(d.get("count", 0)) for d in x_death if isinstance(d, dict)]
        return max(counts) if counts else 0
    except Exception:  # noqa: BLE001
        return 0


__all__ = ["DEFAULT_MAX_RETRIES", "retry_count"]
