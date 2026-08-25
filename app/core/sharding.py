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
from datetime import UTC, datetime

from bili_common.core.snowflake import MinuteSnowflakeIdGenerator, SnowflakeIdGenerator
from loguru import logger
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.core.config import settings
from app.core.database import engine

# ==================== msgkey（毫秒级大容量雪花 ID）====================
# 位布局：| 41 bits 毫秒时间戳(相对 epoch) | 10 bits worker_id | 12 bits 序列号 | = 63 bits
# 由 bili-common 通用 SnowflakeIdGenerator 参数化实例化（time_unit="millisecond"）。

_msgkey_generator = SnowflakeIdGenerator(
    worker_id=settings.msgkey_worker_id,
    epoch=settings.msgkey_epoch_ms,
    timestamp_bits=41,
    worker_bits=10,
    sequence_bits=12,
    time_unit="millisecond",
)


async def generate_msgkey() -> int:
    """生成一个新的 msgkey。"""
    return await _msgkey_generator.next()


# ==================== uid / moment_id / topic_id 雪花 ID 生成器 ====================
# 位布局：| 31 bits 时间戳(分钟, 相对 epoch) | 4 bits worker_id | 4 bits 序列号 | = 39 bits
# 通用生成器在 bili-common/bili_common/core/snowflake.py，各实体独立配置使数值空间分离。
# 每个实体的生成器均使用**不同**的 epoch / worker，避免相互碰撞。

_uid_generator = MinuteSnowflakeIdGenerator(
    worker_id=settings.uid_worker_id,
    epoch_sec=settings.uid_epoch_sec,
    sequence_bits=settings.uid_sequence_bits,
)


async def generate_uid() -> int:
    """生成一个新的用户 uid（短雪花 ID，分钟步进）。"""
    return await _uid_generator.next()


_uid_generator_dyn = MinuteSnowflakeIdGenerator(
    worker_id=settings.moment_id_worker_id,
    epoch_sec=settings.moment_id_epoch_sec,
    sequence_bits=settings.moment_id_sequence_bits,
)


async def generate_moment_id() -> int:
    """生成一个新的动态 ID（短雪花 ID，分钟步进，独立配置空间）。"""
    return await _uid_generator_dyn.next()


_uid_generator_topic = MinuteSnowflakeIdGenerator(
    worker_id=settings.topic_id_worker_id,
    epoch_sec=settings.topic_id_epoch_sec,
    sequence_bits=settings.topic_id_sequence_bits,
)


async def generate_topic_id() -> int:
    """生成一个新的话题 ID（短雪花 ID，分钟步进，独立配置空间）。"""
    return await _uid_generator_topic.next()


def parse_timestamp_ms(msgkey: int) -> int:
    """从 msgkey 反解出毫秒时间戳（分库路由的唯一依据）。"""
    return (msgkey >> _msgkey_generator.timestamp_shift) + settings.msgkey_epoch_ms


def parse_datetime(msgkey: int) -> datetime:
    """从 msgkey 反解出本地时间。"""
    return datetime.fromtimestamp(parse_timestamp_ms(msgkey) / 1000)


# ==================== 分库 / 分表路由 ====================


def db_name_of(msgkey: int) -> str:
    """按 msgkey 内嵌的时间戳解析出所属月度库名。"""
    dt = datetime.fromtimestamp(parse_timestamp_ms(msgkey) / 1000, tz=UTC)
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
    now_key = await generate_msgkey()
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
    "db_name_of",
    "ensure_current_month_shards",
    "ensure_shard",
    "generate_moment_id",
    "generate_msgkey",
    "generate_topic_id",
    "generate_uid",
    "group_by_shard",
    "parse_datetime",
    "parse_timestamp_ms",
    "qualified_table_of",
    "shard_of",
    "table_name_of",
]
