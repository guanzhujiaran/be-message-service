"""测试种子数据源：从 biliopusdb（普通抽奖动态库）拉取真实数据。

be-message 的 Moment 系列单元测试不再手工编造 "seed" / "feed seed" 假文本，
而是直连 be-bilibili-crawler 维护的 MySQL 库 biliopusdb（普通抽奖动态库），
拉取真实动态正文 / 话题名填充测试数据，使测试贴近线上真实形态。

连接参数与 app/.env 的 mysql_message_url 同实例，仅库名换为 biliopusdb。
本模块仅供 tests 使用，不属于运行时依赖。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import aiomysql
from sqlalchemy.engine import make_url

from app.core.config import settings

BILIOPUS_DB = "biliopusdb"


def _biliopus_conn() -> dict:
    """从 mysql_message_url 派生 biliopusdb 的连接参数（同一 MySQL 实例）。"""
    url = make_url(settings.mysql_message_url)
    return {
        "host": url.host or "127.0.0.1",
        "port": url.port or 10000,
        "user": url.username or "root",
        "password": url.password or "",
        "db": BILIOPUS_DB,
    }


@dataclass
class RealDyn:
    """biliopusdb t_lotdyninfo 中的一条真实抽奖动态。"""

    dyn_id: int
    mid: int
    author_name: str
    pub_time: datetime | None
    content_text: str
    like_count: int
    comment_count: int
    repost_count: int


@dataclass
class RealTopic:
    """biliopusdb t_lot_extra_info 中的真实话题。"""

    ref_id: int
    topic_name: str


async def fetch_real_dyns(limit: int = 10, offset: int = 0) -> list[RealDyn]:
    """拉取最新真实动态（优先有正文者）。"""
    conn = await aiomysql.connect(**_biliopus_conn())
    try:
        cur = await conn.cursor()
        await cur.execute(
            "SELECT dynId, up_uid, authorName, pubTime, dynContent, "
            "COALESCE(likeCount, 0), COALESCE(commentCount, 0), COALESCE(repostCount, 0) "
            "FROM t_lotdyninfo "
            "WHERE dynContent IS NOT NULL AND TRIM(dynContent) <> '' "
            "ORDER BY dynId DESC LIMIT %s OFFSET %s",
            (limit, offset),
        )
        rows = await cur.fetchall()
    finally:
        conn.close()
    return [
        RealDyn(
            dyn_id=r[0],
            mid=r[1],
            author_name=r[2],
            pub_time=r[3],
            content_text=r[4],
            like_count=r[5],
            comment_count=r[6],
            repost_count=r[7],
        )
        for r in rows
    ]


async def fetch_real_topics(limit: int = 10) -> list[RealTopic]:
    """拉取真实话题（required_topic_text 非空，按名称去重）。

    兼容 MySQL only_full_group_by：不在 SQL 层 GROUP BY，改为 Python 侧去重。
    """
    conn = await aiomysql.connect(**_biliopus_conn())
    try:
        cur = await conn.cursor()
        await cur.execute(
            "SELECT ref_id, required_topic_text FROM t_lot_extra_info "
            "WHERE required_topic_text IS NOT NULL AND TRIM(required_topic_text) <> '' "
            "ORDER BY ref_id DESC LIMIT %s",
            (limit * 5,),
        )
        rows = await cur.fetchall()
    finally:
        conn.close()
    seen: set[str] = set()
    out: list[RealTopic] = []
    for ref_id, topic_name in rows:
        if topic_name in seen:
            continue
        seen.add(topic_name)
        out.append(RealTopic(ref_id=ref_id, topic_name=topic_name))
        if len(out) >= limit:
            break
    return out
