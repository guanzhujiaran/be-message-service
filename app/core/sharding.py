"""私信内容的「月度分库 + 库内分表」路由实现。

设计目标（对齐 B 站私信系统的存储分层思路）：

1. **冷热数据分离**：私信内容按「消息产生的月份」分库，库名形如
   `bili_msg_content_202608`。历史月份库天然变冷，可整库归档 / 降配 / 卸载，
   不会拖慢当月热库的读写。
2. **流量均衡**：单个月度库内固定 100 张表 `msg_content_00 ~ msg_content_99`，
   由 `msgkey % 100` 决定落在哪张表，把同一个月的写入压力均匀打散，
   避免单表行数暴涨导致 B+ 树层高增加。
3. **路由只依赖 msgkey**：msgkey 是自研雪花 ID，高 41 位即毫秒时间戳，
   因此「解析 msgkey → 得到时间戳 → 得到 YYYYMM → 得到库名」，
   「msgkey % 100 → 得到表名」。读消息时无需任何额外索引即可精确定位物理表。

msgkey 位布局（共 63 位，保证正数）::

    | 41 bits 毫秒时间戳(相对 epoch) | 10 bits worker_id | 12 bits 序列号 |

由于同一实例内 12 位序列号支持每毫秒 4096 条，足以支撑单机私信写入量。
"""

import asyncio
import threading
import time
from datetime import datetime, timezone

from loguru import logger
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.core.config import settings
from app.core.database import engine

# ==================== msgkey 位布局 ====================
_SEQUENCE_BITS = 12
_WORKER_BITS = 10

_MAX_SEQUENCE = (1 << _SEQUENCE_BITS) - 1  # 4095
_MAX_WORKER_ID = (1 << _WORKER_BITS) - 1  # 1023

_WORKER_SHIFT = _SEQUENCE_BITS  # 12
_TIMESTAMP_SHIFT = _SEQUENCE_BITS + _WORKER_BITS  # 22


class MsgKeyGenerator:
    """msgkey 生成器（线程安全的雪花算法实现）。

    生成的 msgkey 全局唯一、单调递增，且可反解出毫秒时间戳，
    这是私信内容能够「按时间分库」的前提。
    """

    def __init__(self, worker_id: int, epoch_ms: int) -> None:
        if not 0 <= worker_id <= _MAX_WORKER_ID:
            raise ValueError(f"worker_id 必须在 0~{_MAX_WORKER_ID} 之间，当前为 {worker_id}")
        self._worker_id = worker_id
        self._epoch_ms = epoch_ms
        self._sequence = 0
        self._last_ts = -1
        self._lock = threading.Lock()

    @staticmethod
    def _now_ms() -> int:
        return int(time.time() * 1000)

    def next_key(self) -> int:
        """生成下一个 msgkey。"""
        with self._lock:
            ts = self._now_ms()
            if ts < self._last_ts:
                # 时钟回拨：等待追平，避免生成重复 ID
                ts = self._last_ts
            if ts == self._last_ts:
                self._sequence = (self._sequence + 1) & _MAX_SEQUENCE
                if self._sequence == 0:
                    # 当前毫秒序列号耗尽，自旋到下一毫秒
                    while ts <= self._last_ts:
                        ts = self._now_ms()
            else:
                self._sequence = 0
            self._last_ts = ts
            return (
                ((ts - self._epoch_ms) << _TIMESTAMP_SHIFT)
                | (self._worker_id << _WORKER_SHIFT)
                | self._sequence
            )


_generator = MsgKeyGenerator(
    worker_id=settings.msgkey_worker_id,
    epoch_ms=settings.msgkey_epoch_ms,
)


def generate_msgkey() -> int:
    """生成一个新的 msgkey。"""
    return _generator.next_key()


# ==================== uid 雪花 ID 生成器（短 ID，分钟步进）====================

# 位布局：| 31 bits 时间戳(分钟单位, 相对 epoch) | 4 bits worker_id | 4 bits 序列号 |
# 总共 39 位，每分钟最多 16 个 uid（单 worker），epoch 设近使初始值约 7~8 位十进制
_UID_SEQUENCE_BITS = 4
_UID_WORKER_BITS = 4

_UID_MAX_SEQUENCE = (1 << _UID_SEQUENCE_BITS) - 1  # 15
_UID_MAX_WORKER_ID = (1 << _UID_WORKER_BITS) - 1  # 15

_UID_WORKER_SHIFT = _UID_SEQUENCE_BITS  # 4
_UID_TIMESTAMP_SHIFT = _UID_SEQUENCE_BITS + _UID_WORKER_BITS  # 8


class UidGenerator:
    """短 uid 生成器（分钟步进雪花算法，线程安全）。

    位布局：31 bits 时间戳 + 4 bits worker + 4 bits 序列号 = 39 bits。
    每分钟最多生成 16 个 uid，超出时自旋等待下一分钟。
    """

    def __init__(self, worker_id: int, epoch_sec: int) -> None:
        if not 0 <= worker_id <= _UID_MAX_WORKER_ID:
            raise ValueError(f"uid worker_id 必须在 0~{_UID_MAX_WORKER_ID}，当前 {worker_id}")
        self._worker_id = worker_id
        # 入参 epoch_sec 是秒级时间戳，但生成器内部以「分钟」为步进单位，
        # 这里统一换算成分钟级 epoch，避免 (ts_minutes - epoch_sec) 单位错配
        # 导致生成负数 uid（参见 ForeignKeyViolationError on TUserDetail.mid）
        self._epoch_minute = epoch_sec // 60
        self._sequence = 0
        self._last_ts = -1
        self._lock = threading.Lock()

    @staticmethod
    def _now_minute() -> int:
        return int(time.time() // 60)

    def next(self) -> int:
        with self._lock:
            ts = self._now_minute()
            if ts < self._last_ts:
                ts = self._last_ts
            if ts == self._last_ts:
                self._sequence = (self._sequence + 1) & _UID_MAX_SEQUENCE
                if self._sequence == 0:
                    # 当前分钟序列号耗尽，自旋到下一分钟
                    while ts <= self._last_ts:
                        ts = self._now_minute()
            else:
                self._sequence = 0
            self._last_ts = ts
            return (
                ((ts - self._epoch_minute) << _UID_TIMESTAMP_SHIFT)
                | (self._worker_id << _UID_WORKER_SHIFT)
                | self._sequence
            )


_uid_generator = UidGenerator(
    worker_id=settings.uid_worker_id,
    epoch_sec=settings.uid_epoch_sec,
)


def generate_uid() -> int:
    """生成一个新的用户 uid（短雪花 ID，分钟步进）。"""
    return _uid_generator.next()


def parse_timestamp_ms(msgkey: int) -> int:
    """从 msgkey 反解出毫秒时间戳（分库路由的唯一依据）。"""
    return (msgkey >> _TIMESTAMP_SHIFT) + settings.msgkey_epoch_ms


def parse_datetime(msgkey: int) -> datetime:
    """从 msgkey 反解出本地时间。"""
    return datetime.fromtimestamp(parse_timestamp_ms(msgkey) / 1000)


# ==================== 分库 / 分表路由 ====================


def db_name_of(msgkey: int) -> str:
    """按 msgkey 内嵌的时间戳解析出所属月度库名。"""
    dt = datetime.fromtimestamp(parse_timestamp_ms(msgkey) / 1000, tz=timezone.utc)
    # 统一按 UTC+8 归月，避免月初 / 月末跨时区导致同一条消息路由到两个库
    dt = dt.astimezone(tz=None)
    return f"{settings.dm_content_db_prefix}_{dt.strftime('%Y%m')}"


def table_name_of(msgkey: int) -> str:
    """按 msgkey 取余路由出库内分表名。"""
    idx = msgkey % settings.dm_content_table_count
    width = len(str(settings.dm_content_table_count - 1))
    return f"{settings.dm_content_table_prefix}_{idx:0{width}d}"


def shard_of(msgkey: int) -> tuple[str, str]:
    """返回 (库名, 表名)。"""
    return db_name_of(msgkey), table_name_of(msgkey)


def qualified_table_of(msgkey: int) -> str:
    """返回 `库名`.`表名` 形式的全限定名，可直接拼进 SQL。"""
    db, table = shard_of(msgkey)
    return f"`{db}`.`{table}`"


def group_by_shard(msgkeys: list[int]) -> dict[str, list[int]]:
    """把一批 msgkey 按物理分片归组，便于批量读取时按分片聚合查询。

    Returns:
        dict: key 为全限定表名，value 为落在该表的 msgkey 列表。
    """
    grouped: dict[str, list[int]] = {}
    for key in msgkeys:
        grouped.setdefault(qualified_table_of(key), []).append(key)
    return grouped


# ==================== 物理表懒创建 ====================

# 已确认存在的分片（全限定名），避免每次写入都执行 DDL 探测
_ensured_shards: set[str] = set()
_ensured_dbs: set[str] = set()
_ensure_lock = asyncio.Lock()

_CREATE_DB_SQL = (
    "CREATE DATABASE IF NOT EXISTS `{db}` "
    "DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
)

# 私信内容表：只存「内容」本体，索引 / 会话关系在主库，读写彻底分离
_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS `{db}`.`{table}` (
    `msgkey`      BIGINT       NOT NULL COMMENT '消息全局唯一键（雪花ID，内嵌毫秒时间戳）',
    `session_key` VARCHAR(64)  NOT NULL COMMENT '会话键：小uid_大uid',
    `sender_uid`  BIGINT       NOT NULL COMMENT '发送者mid',
    `receiver_uid` BIGINT      NOT NULL COMMENT '接收者mid',
    `msg_type`    VARCHAR(16)  NOT NULL DEFAULT 'text' COMMENT '消息类型',
    `content`     MEDIUMTEXT   NULL COMMENT '消息内容体',
    `msg_ts`      BIGINT       NOT NULL COMMENT '消息毫秒时间戳',
    `created_at`  DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (`msgkey`),
    KEY `idx_session_msgkey` (`session_key`, `msgkey`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='私信内容分表（月度分库+库内100表）'
"""


async def _ensure_shard_on_conn(conn: AsyncConnection, db: str, table: str) -> None:
    if db not in _ensured_dbs:
        await conn.execute(text(_CREATE_DB_SQL.format(db=db)))
        _ensured_dbs.add(db)
    await conn.execute(text(_CREATE_TABLE_SQL.format(db=db, table=table)))


async def ensure_shard(msgkey: int) -> str:
    """确保 msgkey 对应的月度库与分表已存在，返回全限定表名。

    采用「懒创建」策略：不预先建出 12 个月 × 100 张空表，
    只有真正写到某个分片时才建，对小设备的元数据开销最友好。
    DDL 结果缓存在进程内，同一分片只会执行一次。
    """
    db, table = shard_of(msgkey)
    qualified = f"`{db}`.`{table}`"
    if qualified in _ensured_shards:
        return qualified
    async with _ensure_lock:
        if qualified in _ensured_shards:
            return qualified
        async with engine.begin() as conn:
            await _ensure_shard_on_conn(conn, db, table)
        _ensured_shards.add(qualified)
        logger.info(f"私信内容分片已就绪: {qualified}")
    return qualified


async def ensure_current_month_shards() -> None:
    """预热当月分片：启动时把当前月份的 100 张表一次性建好。

    这样首条私信写入时无需承担 DDL 耗时；历史月份库仍保持懒创建。
    """
    now_key = generate_msgkey()
    db = db_name_of(now_key)
    width = len(str(settings.dm_content_table_count - 1))
    async with engine.begin() as conn:
        await conn.execute(text(_CREATE_DB_SQL.format(db=db)))
        _ensured_dbs.add(db)
        for idx in range(settings.dm_content_table_count):
            table = f"{settings.dm_content_table_prefix}_{idx:0{width}d}"
            await conn.execute(text(_CREATE_TABLE_SQL.format(db=db, table=table)))
            _ensured_shards.add(f"`{db}`.`{table}`")
    logger.info(f"当月私信内容库 {db} 已预热 {settings.dm_content_table_count} 张分表")


__all__ = [
    "MsgKeyGenerator",
    "generate_msgkey",
    "parse_timestamp_ms",
    "parse_datetime",
    "db_name_of",
    "table_name_of",
    "shard_of",
    "qualified_table_of",
    "group_by_shard",
    "ensure_shard",
    "ensure_current_month_shards",
]
