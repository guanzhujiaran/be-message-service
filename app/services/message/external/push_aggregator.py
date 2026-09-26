"""推送「首条直推 + 冷却期内聚合」治理策略（按 接收人 × 标题 分桶）。

为什么需要
==========

所有服务的「站外推送」都汇聚到本服务的 `message.push` 消费者
（见 ``app/consumers/external_push.py``）。若不做任何治理，批量故障时会被刷爆 ——
例如 crawler 的消费失败告警，一次可能几百条（同队列、同异常）。

策略（leading-edge + trailing batch）
=====================================

按 ``(接收人, 标题)`` 分桶：

1. 桶空闲（距上次推送 >= 冷却期）→ **立即推送**（首条零延迟）；
2. 桶处于冷却期 → 不推送，进缓冲；
3. 冷却到期 → 缓冲按内容去重计数，渲染成**一条摘要**推送 → 重置冷却；
4. 冷却到期且缓冲为空 → 回收该桶，下次同类推送可立即直推。

分桶 key
========

``(recipient_key(merge_config(message)), message.title)``

- ``recipient_key`` 用 ``merge_config(message)`` **合并后实际生效**的配置算摘要：
  这样「消息里带 config」与「回落全局配置」只要最终凭据一致，就落同一个桶；
  用摘要而非明文，避免密钥出现在内存 key / 日志里；
- **不同密钥 = 不同接收人**，因此不同密钥的推送永远不会被合并进同一条摘要；
- 用**标题**做二级分桶，使同一接收人的不同类别告警（如「MQ消费失败」与「服务异常」）
  各占一个冷却窗口、互不挤占，摘要标题也能如实反映内容类别。

参与范围（内部约定，不涉及 MQ 契约）
====================================

只有**失败主题**（标题以 ``[f]`` 开头，即各服务 ``PushSubject.FAILURE``）的推送参与聚合。
业务通知（``[i]``/``[s]``/``[w]``，含 RPA-Browser 的 per-user 推送）保持 1:1 直推 ——
它们不该被合并，也不该被冷却期延迟。

已知限制
========

桶状态在**进程内存**中（本服务当前单实例部署，docker-compose 无 replicas）：
容器重启会丢掉冷却期内的缓冲，靠 ``stop_push_aggregator`` 的关闭 flush 兜住大部分。
将来若水平扩容 / 多副本，需把 ``_buckets`` 换成 Redis 等共享存储。
"""

import asyncio
import time
from collections import Counter
from contextlib import suppress
from dataclasses import dataclass, field
from hashlib import md5
from typing import Sequence

from loguru import logger

from app.core.config import settings
from app.services.message.external.push import PushMessageService
from app.services.message.external.push_helper import merge_config
from bili_common.models.push import PushChannelConfig, PushMessagePayload

#: 「失败」主题标识（标题前缀）：与各服务 ``PushMe.PushSubject.FAILURE`` 一致。
#: 只有失败类推送参与聚合，业务通知 / per-user 推送保持 1:1 直推。
_FAILURE_TOPIC = "[f]"
#: 聚合摘要里单条内容的展示长度：超出截断，避免一条超长消息独占整条摘要
_ITEM_LIMIT = 200
#: 尾部「已省略 N 条」提示预留的字符数
_TAIL_RESERVE = 60
#: 摘要正文下限：配置成极小值时保证摘要仍可读
_MIN_CONTENT_LEN = 200
#: 冷却期下限（秒）：过小的冷却会退化成「每条都直推」
_MIN_COOLDOWN_SECONDS = 1.0
#: 扫描间隔下限（秒）
_MIN_SWEEP_SECONDS = 0.5


@dataclass(slots=True)
class _Bucket:
    """一个 ``(接收人, 标题)`` 桶的状态。"""

    #: 上次推送完成的时间（monotonic），用于判断冷却
    last_push_at: float
    #: 桶对应的推送标题（同桶同标题，摘要直接沿用）
    title: str
    push_type: str | None
    #: 合并后的生效配置（与桶 key 的 recipient_key 对应）
    config: PushChannelConfig
    #: 本轮聚合窗口的起点（**墙上时间** ``time.time()``，仅用于摘要里展示时间范围）
    started_at: float
    #: 冷却期内缓冲的推送正文
    buffer: list[str] = field(default_factory=list)


#: 桶表：``(recipient_key, title) -> _Bucket``（进程内存，见模块 docstring 的已知限制）
_buckets: dict[tuple[str, str], _Bucket] = {}
_sweep_task: asyncio.Task | None = None


# ============================================================================
# 配置读取（每次调用都读，便于运行期调整 / 测试覆盖）
# ============================================================================
def _cooldown_seconds() -> float:
    return max(_MIN_COOLDOWN_SECONDS, settings.push_aggregate_cooldown_seconds)


def _max_content_len() -> int:
    return max(_MIN_CONTENT_LEN, settings.push_aggregate_max_len)


def _sweep_seconds() -> float:
    return max(_MIN_SWEEP_SECONDS, settings.push_aggregate_sweep_seconds)


# ============================================================================
# 分桶 key / 参与范围
# ============================================================================
def recipient_key(config: PushChannelConfig) -> str:
    """接收人标识：配置的稳定摘要（**不含明文密钥**）。

    入参必须是 ``merge_config(message)`` 之后「实际生效」的配置 ——
    这样「消息里带 config」与「回落全局配置」只要最终凭据一致，就落同一个桶。
    """
    return md5(config.model_dump_json().encode("utf-8")).hexdigest()


def bucket_key(config: PushChannelConfig, title: str) -> tuple[str, str]:
    """桶 key：``(接收人标识, 推送标题)``。"""
    return recipient_key(config), title


def should_aggregate(title: str) -> bool:
    """是否参与聚合：只聚合**失败主题**（标题以 ``[f]`` 开头）的推送。

    这是内部约定、不改任何 MQ 契约：业务通知（``[i]``/``[s]``/``[w]``）与 per-user
    推送必须保持 1:1 直推，否则同一接收人冷却期内的多条通知会被合并成摘要。
    """
    return title.startswith(_FAILURE_TOPIC)


# ============================================================================
# 摘要渲染（长度一定 <= max_len）
# ============================================================================
def _clip(text: str, limit: int = _ITEM_LIMIT) -> str:
    """折叠空白后截断：摘要里换行会让正文行数爆炸。"""
    flat = " ".join(str(text).split())
    if len(flat) <= limit:
        return flat
    return f"{flat[:limit]}…"


def build_digest_content(
    contents: Sequence[str],
    *,
    started_at: float,
    ended_at: float,
    max_len: int,
) -> str:
    """把冷却期内缓冲的多条推送合成一条摘要。

    内容按去重后的出现次数降序展示（``×N``），超出长度预算则停止并提示省略条数，
    最后再做一次硬截断兜底。
    """
    counter = Counter(contents)
    total = len(contents)
    header = (
        f"冷却期内合并 {total} 条推送"
        f"（{_fmt_time(started_at)} ~ {_fmt_time(ended_at)}）\n"
    )

    budget = max_len - len(header) - _TAIL_RESERVE
    items = counter.most_common()
    lines: list[str] = []
    used = 0
    omitted = 0
    for index, (content, count) in enumerate(items):
        line = f"\n· ×{count}  {_clip(content)}"
        if used + len(line) > budget:
            omitted = sum(rest_count for _, rest_count in items[index:])
            break
        used += len(line)
        lines.append(line)

    tail = f"\n…已省略 {omitted} 条（内容过长）" if omitted else ""
    digest = header + "".join(lines) + tail
    if len(digest) > max_len:
        digest = f"{digest[: max_len - 1]}…"
    return digest


def _fmt_time(timestamp: float) -> str:
    """墙上时间戳 → ``HH:MM:SS``（仅用于摘要里展示聚合窗口）。"""
    return time.strftime("%H:%M:%S", time.localtime(timestamp))


# ============================================================================
# 发送
# ============================================================================
async def _send(service: PushMessageService, title: str, content: str) -> bool:
    """发一条推送；失败只记日志、不重投（站外提醒属尽力而为，与历史行为一致）。"""
    try:
        return await service.send(title, content)
    except Exception as e:  # noqa: BLE001
        logger.error(f"推送失败，消息丢弃不重投 title={title} push_type={service.push_type}: {e}")
        return False


async def deliver(message: PushMessagePayload) -> bool:
    """聚合策略入口（消费者调用）。

    Returns:
        本次是否**真的推送出去了**（进入缓冲时返回 False，属正常情况）。
    """
    config = merge_config(message)
    now = time.monotonic()

    # 非失败主题（业务通知 / per-user 推送）：不聚合，1:1 直推
    if not should_aggregate(message.title):
        return await _send(
            PushMessageService(config, push_type=message.push_type),
            message.title,
            message.content,
        )

    key = bucket_key(config, message.title)
    bucket = _buckets.get(key)

    if bucket is None:
        # 桶空闲 → 首条立即推送（零延迟），并开启本轮冷却
        sent = await _send(
            PushMessageService(config, push_type=message.push_type),
            message.title,
            message.content,
        )
        if sent:
            _buckets[key] = _Bucket(
                last_push_at=now,
                title=message.title,
                push_type=message.push_type,
                config=config,
                started_at=time.time(),
            )
        return sent

    # 桶处于冷却期（或扫描器还没来得及 flush）→ 进缓冲，由扫描/本次到期判定统一发摘要
    bucket.buffer.append(message.content)
    if now - bucket.last_push_at >= _cooldown_seconds():
        await _flush_bucket(key, bucket, now=now)
    return False


async def _flush_bucket(key: tuple[str, str], bucket: _Bucket, *, now: float) -> bool:
    """把某个桶的缓冲发成一条摘要，并重置冷却。"""
    contents = list(bucket.buffer)
    bucket.buffer.clear()
    if not contents:
        return False

    digest = build_digest_content(
        contents,
        started_at=bucket.started_at,
        ended_at=time.time(),
        max_len=_max_content_len(),
    )
    sent = await _send(
        PushMessageService(bucket.config, push_type=bucket.push_type),
        bucket.title,
        digest,
    )
    bucket.last_push_at = now
    bucket.started_at = time.time()
    if sent:
        logger.info(
            f"推送聚合摘要已发送：title={key[1]!r} 合并 {len(contents)} 条 "
            f"recipient={key[0][:8]}…"
        )
    return sent


# ============================================================================
# 到期扫描 / 生命周期
# ============================================================================
async def flush_due_buckets() -> int:
    """扫描到期桶：有缓冲的发摘要，空桶直接回收（让下次同类推送可立即直推）。"""
    now = time.monotonic()
    cooldown = _cooldown_seconds()
    flushed = 0
    for key, bucket in list(_buckets.items()):
        if now - bucket.last_push_at < cooldown:
            continue
        if not bucket.buffer:
            _buckets.pop(key, None)
            continue
        if await _flush_bucket(key, bucket, now=now):
            flushed += 1
    return flushed


async def flush_all_buckets() -> int:
    """忽略冷却，把所有缓冲立刻发出去（服务关闭时调用，避免丢掉最后一批）。"""
    now = time.monotonic()
    flushed = 0
    for key, bucket in list(_buckets.items()):
        if not bucket.buffer:
            continue
        if await _flush_bucket(key, bucket, now=now):
            flushed += 1
    return flushed


async def _sweep_loop() -> None:
    while True:
        await asyncio.sleep(_sweep_seconds())
        try:
            await flush_due_buckets()
        except Exception as e:  # noqa: BLE001 - 扫描循环不能因单次异常退出
            logger.opt(exception=e).error(f"推送聚合到期扫描失败：{e}")


async def start_push_aggregator() -> None:
    """启动到期扫描循环（由 broker 启动钩子调用）。"""
    global _sweep_task
    if _sweep_task is not None and not _sweep_task.done():
        return
    _sweep_task = asyncio.create_task(_sweep_loop())


async def stop_push_aggregator() -> None:
    """停掉扫描循环，并把冷却期内已聚合但还没发出去的摘要补发一次。"""
    global _sweep_task
    task = _sweep_task
    _sweep_task = None
    if task is not None and not task.done():
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
    try:
        await flush_all_buckets()
    except Exception as e:  # noqa: BLE001 - 关闭流程不应因补发失败而报错
        logger.error(f"关闭前补发推送聚合摘要失败：{e}")


def reset_push_aggregator() -> None:
    """清空桶状态（测试用）。"""
    _buckets.clear()


__all__ = [
    "bucket_key",
    "build_digest_content",
    "deliver",
    "flush_all_buckets",
    "flush_due_buckets",
    "recipient_key",
    "reset_push_aggregator",
    "should_aggregate",
    "start_push_aggregator",
    "stop_push_aggregator",
]
