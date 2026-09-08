"""外部**只读**数据源：biliopusdb / bilidb 直连、pptr Postgres 用户池、主库已有话题。

只读取，不写这些库（seed 严禁直写 MySQL，写操作一律经 be-message HTTP 接口，见计划书 C14）。
"""
import aiomysql
from loguru import logger
from sqlalchemy import func
from sqlalchemy.engine import make_url
from sqlmodel import col, select

from app.core.config import settings
from app.core.database import new_pptr_session
from app.models.pptr_db import PptrUserDetail, PptrUserInfo


def _raw_conn(db: str) -> dict:
    """从 mysql_message_url 派生指定库的连接参数（同一 MySQL 实例）。"""
    url = make_url(settings.mysql_message_url)
    return {
        "host": url.host or "127.0.0.1",
        "port": url.port or 10000,
        "user": url.username or "root",
        "password": url.password or "",
        "db": db,
    }


BILIOPUS_DB = "biliopusdb"


def _biliopus_conn() -> dict:
    """从 mysql_message_url 派生 biliopusdb 的连接参数（同一 MySQL 实例）。"""
    return _raw_conn(BILIOPUS_DB)


#: bilidb 库名（素材池话题名来源：t_topic_item）
BILIDB = "bilidb"


async def _fetch_real_users(limit: int) -> list[tuple[int, str | None]]:
    """从 pptr Postgres 随机取若干真实用户 (uid, uname) —— 只读，不写 pptr。"""
    async with new_pptr_session() as s:
        stmt = (
            select(PptrUserInfo.uid, PptrUserDetail.uname)
            .join(
                PptrUserDetail,
                col(PptrUserDetail.mid) == col(PptrUserInfo.uid),
                isouter=True,
            )
            .where(col(PptrUserInfo.deletedAt).is_(None))
            .where(col(PptrUserDetail.uname).isnot(None))
            .order_by(func.random())
            .limit(limit)
        )
        rows = (await s.exec(stmt)).all()
    users = [(int(uid), uname) for uid, uname in rows]
    if not users:
        logger.warning("pptr 库未取到任何真实用户，请确认 pptr 数据库连接与数据。")
    return users


async def fetch_pptr_user_pool(size: int) -> list[tuple[int, str | None]]:
    """从自有用户系统（pptr Postgres）随机取 size 个真实用户 ``(uid, uname)``。

    用作动态作者 / 点赞者 / 浏览者池：外库动态仅保留话题与内容，
    所有者信息统一链接到本地用户系统，保证 Feed 回查 author 信息完整。
    昵称一并取出，供正文末尾追加随机 @ 使用（动态 AT 节点需要 ``name``）。
    """
    async with new_pptr_session() as s:
        stmt = (
            select(PptrUserInfo.uid, PptrUserDetail.uname)
            .join(
                PptrUserDetail,
                col(PptrUserDetail.mid) == col(PptrUserInfo.uid),
                isouter=True,
            )
            .where(col(PptrUserInfo.deletedAt).is_(None))
            .where(col(PptrUserDetail.uname).isnot(None))
            .order_by(func.random())
            .limit(size)
        )
        rows = (await s.exec(stmt)).all()
    users = [(int(uid), uname) for uid, uname in rows]
    if not users:
        logger.error(
            "pptr 库未取到任何真实用户 uid，无法作为动态作者，请确认 pptr 数据库连接与数据。"
        )
    return users


async def fetch_real_dyns(total: int) -> list[tuple]:
    """从 biliopusdb 流式拉取真实动态（keyset 分页，避免 OFFSET 深翻页慢）。

    仅取**话题与动态内容**相关字段，返回 (dynId, pubTime, dynContent, commentCount, repostCount)。
    不取外库作者 up_uid —— 灌入时作者统一从自有用户系统（pptr Postgres）随机映射。
    仅取 dynId>0 且 pubTime>2000-01-01 且正文非空的有效动态。
    """
    conn = await aiomysql.connect(**_biliopus_conn())
    out: list[tuple] = []
    try:
        cur = await conn.cursor()
        last_dyn_id: int | None = None
        page = 2000
        while len(out) < total:
            if last_dyn_id is None:
                await cur.execute(
                    "SELECT dynId, pubTime, dynContent, "
                    "COALESCE(commentCount,0), COALESCE(repostCount,0) "
                    "FROM t_lotdyninfo "
                    "WHERE dynId > 0 AND pubTime > '2000-01-01' "
                    "AND dynContent IS NOT NULL AND TRIM(dynContent) <> '' "
                    "ORDER BY dynId DESC LIMIT %s",
                    (page,),
                )
            else:
                await cur.execute(
                    "SELECT dynId, pubTime, dynContent, "
                    "COALESCE(commentCount,0), COALESCE(repostCount,0) "
                    "FROM t_lotdyninfo "
                    "WHERE dynId > 0 AND pubTime > '2000-01-01' "
                    "AND dynContent IS NOT NULL AND TRIM(dynContent) <> '' "
                    "AND dynId < %s "
                    "ORDER BY dynId DESC LIMIT %s",
                    (last_dyn_id, page),
                )
            rows = await cur.fetchall()
            if not rows:
                break
            out.extend(rows)
            last_dyn_id = rows[-1][0]
            logger.info(f"  已拉取真实动态 {len(out)} / {total}")
    finally:
        conn.close()
    return out[:total]


async def fetch_real_topics() -> list[str]:
    """拉取真实话题名（required_topic_text 非空，去重）。"""
    conn = await aiomysql.connect(**_biliopus_conn())
    try:
        cur = await conn.cursor()
        await cur.execute(
            "SELECT DISTINCT required_topic_text FROM t_lot_extra_info "
            "WHERE required_topic_text IS NOT NULL AND TRIM(required_topic_text) <> '' "
            "ORDER BY required_topic_text LIMIT 5000"
        )
        rows = await cur.fetchall()
    finally:
        conn.close()
    # 去除首尾空白（含全角/Unicode 空格，MySQL TRIM 不处理但 Python strip 能去），
    # 与 create_topic 接口对 topicName 的 strip 规范化保持一致，避免幂等复用比对失配。
    return list(dict.fromkeys(r[0].strip() for r in rows if r[0] and r[0].strip()))


async def _load_existing_topics() -> dict[str, int]:
    """直连 be-message MySQL 只读 TMomentTopic，建立 topicName->topicId 映射（幂等复用）。

    库里已有话题可能由不同 admin_mid 创建，``/topic/mine`` 按创建者过滤会漏掉，
    故直接读主库确认前置数据存在（只读不写），用于幂等复用，避免重复话题 422 后被丢弃导致动态无法挂话题。
    """
    url = make_url(settings.mysql_message_url)
    conn = await aiomysql.connect(
        host=url.host,
        port=url.port,
        user=url.username,
        password=url.password,
        db=url.database,
    )
    try:
        cur = await conn.cursor()
        await cur.execute("SELECT topicName, topicId FROM TMomentTopic")
        rows = await cur.fetchall()
    finally:
        conn.close()
    return {str(r[0]): int(r[1]) for r in rows}
