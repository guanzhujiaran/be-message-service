"""私信内容的分库分表读写。

这一层是「冷热分离 + 流量均衡」落地的地方：

- 写：由 msgkey 解析出月度库与分表，`CREATE ... IF NOT EXISTS` 懒建后 INSERT。
- 读：把一批 msgkey 按物理分片归组，每个分片一条 `WHERE msgkey IN (...)`，
  N 张表最多 N 次查询，且每次都是主键批量点查。

刻意绕开 ORM 直接用原生 SQL + engine 连接，原因有二：
1. 分表是运行时才确定的物理表名，无法用 SQLModel 静态声明 100 × N 个类。
2. 内容写入与主库索引写入本来就要求「不同事务、可异步」，
   共用 ORM Session 反而会把它们绑进同一个事务，违背异步化的初衷。

历史月份库不存在（比如查询一条早于服务上线的消息）时，
读取只会返回空内容而不会抛错，由上层用索引表的 `content_preview` 兜底。
"""

from loguru import logger
from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError

from app.core.database import engine
from app.core.sharding import ensure_shard, group_by_shard, qualified_table_of
from app.models.schemas import DmContentPayload

_INSERT_SQL = """
INSERT INTO {table}
    (`msgkey`, `session_key`, `sender_uid`, `receiver_uid`, `msg_type`, `content`, `msg_ts`)
VALUES
    (:msgkey, :session_key, :sender_uid, :receiver_uid, :msg_type, :content, :msg_ts)
ON DUPLICATE KEY UPDATE `content` = VALUES(`content`)
"""


class DmContentService:
    """私信正文在分库分表上的持久化操作。"""

    @staticmethod
    async def write(payload: DmContentPayload) -> bool:
        """把一条私信正文写入其所属分片。

        使用 `ON DUPLICATE KEY UPDATE` 保证幂等：MQ 重投同一条消息时
        不会报主键冲突，也不会产生重复正文。
        """
        table = await ensure_shard(payload.msgkey)
        try:
            async with engine.begin() as conn:
                await conn.execute(
                    text(_INSERT_SQL.format(table=table)),
                    {
                        "msgkey": payload.msgkey,
                        "session_key": payload.session_key,
                        "sender_uid": payload.sender_uid,
                        "receiver_uid": payload.receiver_uid,
                        "msg_type": str(payload.msg_type),
                        "content": payload.content,
                        "msg_ts": payload.msg_ts,
                    },
                )
            return True
        except Exception as e:  # noqa: BLE001
            logger.error(f"私信内容写入分片 {table} 失败 msgkey={payload.msgkey}: {e}")
            return False

    @staticmethod
    async def batch_get(msgkeys: list[int]) -> dict[int, str]:
        """批量读取正文，返回 {msgkey: content}。

        按分片归组后逐片查询，缺失的 msgkey 不会出现在结果里，
        调用方据此判断是否需要回落到摘要。
        """
        if not msgkeys:
            return {}
        result: dict[int, str] = {}
        grouped = group_by_shard(msgkeys)
        async with engine.connect() as conn:
            for table, keys in grouped.items():
                placeholders = ", ".join(f":k{i}" for i in range(len(keys)))
                params = {f"k{i}": key for i, key in enumerate(keys)}
                sql = f"SELECT `msgkey`, `content` FROM {table} WHERE `msgkey` IN ({placeholders})"  # noqa: S608
                try:
                    rows = (await conn.execute(text(sql), params)).all()
                except ProgrammingError:
                    # 分片不存在（历史月份库尚未创建）：视为无内容，交由摘要兜底
                    logger.debug(f"私信内容分片 {table} 不存在，跳过")
                    continue
                except Exception as e:  # noqa: BLE001
                    logger.warning(f"读取私信内容分片 {table} 失败: {e}")
                    continue
                for msgkey, content in rows:
                    result[int(msgkey)] = content or ""
        return result

    @staticmethod
    async def clear_content(msgkey: int) -> bool:
        """清空某条消息的正文（撤回时调用，物理层面抹掉内容）。"""
        table = qualified_table_of(msgkey)
        sql = f"UPDATE {table} SET `content` = NULL WHERE `msgkey` = :msgkey"  # noqa: S608
        try:
            async with engine.begin() as conn:
                await conn.execute(text(sql), {"msgkey": msgkey})
            return True
        except ProgrammingError:
            # 分片不存在说明内容还没落库，撤回本身依然成立
            return True
        except Exception as e:  # noqa: BLE001
            logger.warning(f"清空私信内容失败 msgkey={msgkey}: {e}")
            return False


__all__ = ["DmContentService"]
