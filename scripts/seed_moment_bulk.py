"""性能测试 / 联调大数据灌数脚本（Phase 8 P8-T10，直写 MySQL 批量灌数版）。

从 biliopusdb（普通抽奖动态库，be-bilibili-crawler 维护，约 52.5 万条有效动态）
流式拉取**真实**动态正文 / 话题 / 评论与转发计数（**不含外库作者 up_uid**），灌入
BiliMessageDB 的全 Moment 相关表（TMoment 主表 + TMomentStat 1:1 + TMomentTopic +
TMomentLike + TMomentViewLog + TMomentAuditLog），默认 **10 万**规模。

作者关联策略：外库动态仅保留话题与内容，所有者（mid）统一链接到自有用户系统
（pptr Postgres），灌入时对每条动态随机选取一个本地用户作为 author / 点赞者 /
浏览者，保证 Feed 接口回查 author 用户信息完整。

用途：
- P7-T6 性能测试：Feed 流分页、批量详情、Stat 批量 IN 查询 SQL EXPLAIN、
  对账脚本（reconcile_moment_stats.py）对大数据量 COUNT 表现验证；
- 开发环境联调：前后端联调时有接近线上的数据量。

区别于 scripts/seed_moment_via_api.py（纯 HTTP 接口调用版，走完整业务链路、
数据量小、校验严格）：本脚本**直写数据库**，数据量大、速度快，绕过业务校验，
仅保证与线上一致的表结构 / 枚举 / 索引形态。

数据一致性（对账友好）：
- TMomentStat.likeCount 与 TMomentLike 明细数严格对齐；
- TMomentStat.viewCount 与 TMomentViewLog 明细行数严格对齐；
- TMomentStat.commentCount / repostCount 沿用 biliopusdb 真实值。

用法（be-message-service 目录下）：
    uv run python scripts/seed_moment_bulk.py --count 100000
    uv run python scripts/seed_moment_bulk.py --count 100000 --clean
    uv run python scripts/seed_moment_bulk.py --count 1000 --dry-run
"""

from __future__ import annotations

import argparse
import asyncio
import datetime
import random
import sys
from pathlib import Path

# 允许直接 `uv run python scripts/seed_moment_bulk.py` 运行（不依赖手动 PYTHONPATH）
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import aiomysql
from loguru import logger
from sqlalchemy import func, select, text
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.engine import make_url
from sqlmodel import col, delete

from app.core.config import settings
from app.core.database import new_pptr_session, new_session
from app.models.db.moment import (
    TMoment,
    TMomentAuditLog,
    TMomentLike,
    TMomentStat,
    TMomentTopic,
    TMomentViewLog,
)
from app.models.enums import (
    MomentAuditLogActionEnum,
    MomentAuditLogOperatorRoleEnum,
    MomentAuditStatusEnum,
    MomentFoldTypeEnum,
    MomentTypeEnum,
    MomentVisibleScopeEnum,
)
from app.models.pptr_db import PptrUserDetail, PptrUserInfo

BILIOPUS_DB = "biliopusdb"

# 清空顺序（FK 依赖：先删明细，再删主表/统计，最后话题）
MOMENT_TABLES = [
    "TMomentLike",
    "TMomentViewLog",
    "TMomentReport",
    "TMomentAuditLog",
    "TMomentStat",
    "TMoment",
    "TMomentTopic",
]

# 审核状态分布（normal 占绝大多数供 Feed/详情压测；少量 auditing/rejected 供过滤逻辑验证）
AUDIT_DISTRIBUTION = [
    (MomentAuditStatusEnum.NORMAL, 90),
    (MomentAuditStatusEnum.AUDITING, 5),
    (MomentAuditStatusEnum.REJECTED, 5),
]
# 点赞数分布（近似幂律：多数低赞、少数高赞），均值约 4 / 条
LIKE_DISTRIBUTION = [
    (0, 70),
    (1, 12),
    (3, 8),
    (8, 5),
    (20, 3),
    (50, 1.5),
    (120, 0.5),
]
# 浏览量分布（近似幂律），均值约 3 / 条
VIEW_DISTRIBUTION = [
    (0, 55),
    (1, 15),
    (3, 12),
    (8, 8),
    (20, 5),
    (60, 3),
    (150, 1.5),
    (400, 0.5),
]
# 话题关联概率（部分动态挂话题，压测 topic_feed 索引）
TOPIC_LINK_RATIO = 0.5
# 空间置顶比例
TOP_RATIO = 0.01


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


def _sample(distribution: list[tuple[int, float]]) -> int:
    """按分布随机取值（distribution = [(值, 权重), ...]）。"""
    values, weights = zip(*distribution)
    return random.choices(values, weights=weights, k=1)[0]


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


async def fetch_pptr_user_pool(size: int) -> list[int]:
    """从自有用户系统（pptr Postgres）随机取 size 个真实用户 uid。

    用作动态作者 / 点赞者 / 浏览者池：外库动态仅保留话题与内容，
    所有者信息统一链接到本地用户系统，保证 Feed 回查 author 信息完整。
    """
    async with new_pptr_session() as s:
        stmt = (
            select(PptrUserInfo.uid)
            .join(PptrUserDetail, PptrUserDetail.mid == PptrUserInfo.uid, isouter=True)
            .where(col(PptrUserInfo.deletedAt).is_(None))
            .where(col(PptrUserDetail.uname).isnot(None))
            .order_by(func.random())
            .limit(size)
        )
        rows = (await s.exec(stmt)).all()
    uids = [int(r[0]) for r in rows]
    if not uids:
        logger.error("pptr 库未取到任何真实用户 uid，无法作为动态作者，请确认 pptr 数据库连接与数据。")
    return uids


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
    return [r[0] for r in rows]


async def clean_tables() -> None:
    """清空全部 Moment 相关表（供 --clean 一键重建）。"""
    async with new_session() as session:
        for table in MOMENT_TABLES:
            model = {
                "TMomentLike": TMomentLike,
                "TMomentViewLog": TMomentViewLog,
                "TMomentReport": None,  # 需要时再导入，见下
                "TMomentAuditLog": TMomentAuditLog,
                "TMomentStat": TMomentStat,
                "TMoment": TMoment,
                "TMomentTopic": TMomentTopic,
            }[table]
            if model is None:
                from app.models.db.moment import TMomentReport

                model = TMomentReport
            result = await session.exec(delete(model))
            logger.info(f"  清空 {table}: {result.rowcount} 行")
        await session.commit()


def _audit_status() -> MomentAuditStatusEnum:
    values = [s for s, _ in AUDIT_DISTRIBUTION]
    weights = [w for _, w in AUDIT_DISTRIBUTION]
    return random.choices(values, weights=weights, k=1)[0]


async def main() -> None:
    parser = argparse.ArgumentParser(description="Moment 大数据灌数（直写 MySQL）")
    parser.add_argument("--count", type=int, default=100000, help="TMoment 主表灌入条数（默认 100000）")
    parser.add_argument("--clean", action="store_true", help="先清空全部 Moment 相关表再灌")
    parser.add_argument("--batch-size", type=int, default=2000, help="每批插入行数（默认 2000）")
    parser.add_argument("--dry-run", action="store_true", help="只打印计划，不写库")
    parser.add_argument("--liker-pool-size", type=int, default=20000, help="点赞者池大小（默认 20000）")
    parser.add_argument("--viewer-pool-size", type=int, default=10000, help="浏览者池大小（默认 10000）")
    args = parser.parse_args()

    logger.info(
        f"灌数计划：count={args.count}, clean={args.clean}, batch={args.batch_size}, dry_run={args.dry_run}"
    )
    if args.count < 1:
        logger.error("count 必须 >= 1")
        sys.exit(2)

    # 1. 拉取数据源
    logger.info("拉取 biliopusdb 真实数据…")
    reals = await fetch_real_dyns(args.count)
    if len(reals) < args.count:
        logger.warning(f"biliopusdb 有效动态仅 {len(reals)} 条，按实际数量灌入")
    if not reals:
        logger.error("biliopusdb 无有效数据，中止")
        sys.exit(1)
    topic_names = await fetch_real_topics()
    logger.info(f"真实动态 {len(reals)} 条 / 真实话题 {len(topic_names)} 个")

    if args.dry_run:
        like_total = sum(_sample(LIKE_DISTRIBUTION) for _ in reals)
        view_total = sum(_sample(VIEW_DISTRIBUTION) for _ in reals)
        logger.info(
            f"[dry-run] 将灌入：TMoment {len(reals)}、TMomentStat {len(reals)}、"
            f"TMomentTopic {len(topic_names)}、TMomentLike 约 {like_total}、"
            f"TMomentViewLog 约 {view_total}、TMomentAuditLog 约 {len(reals)}；不执行写入"
        )
        return

    # 2. 清理
    if args.clean:
        logger.info("清空已有 Moment 相关表…")
        await clean_tables()

    rng = random.Random(20260815)
    # 作者 / 点赞者 / 浏览者统一取自自有用户系统（pptr Postgres）。
    # 外库动态仅保留话题与内容，所有者信息随机映射到本地用户，保证 Feed 回查 author 完整。
    author_pool = await fetch_pptr_user_pool(args.liker_pool_size)
    if not author_pool:
        logger.error("pptr 无可用用户 uid，无法映射动态作者，中止")
        sys.exit(1)
    logger.info(f"自有用户池（pptr）{len(author_pool)} 个，用作作者/点赞者/浏览者")

    # 3. 灌入
    async with new_session() as session:
        # 3.1 话题（批量 add_all + 一次 flush，替代逐行 flush）
        logger.info("灌入 TMomentTopic…")
        topic_ids: list[int] = []
        topics = [TMomentTopic(topicName=name) for name in topic_names]
        session.add_all(topics)
        await session.flush()
        topic_ids = [t.topicId for t in topics]
        await session.commit()
        logger.info(f"  TMomentTopic {len(topic_ids)} 个")

        # 3.2 主表 + 统计（1:1），like/view 明细计划一起规划
        # 批量写入用 `insert(Model)` + dict 列表（executemany 多值 INSERT），
        # 远快于 ORM add_all 逐行 INSERT（aiomysql 对 ORM 批量支持差）
        like_plan: list[tuple[int, int]] = []  # (dynId, like_target)
        view_plan: list[tuple[int, int]] = []  # (dynId, view_target)
        audit_logs: list[dict] = []
        total_main = 0
        for i in range(0, len(reals), args.batch_size):
            chunk = reals[i : i + args.batch_size]
            moment_rows: list[dict] = []
            stat_rows: list[dict] = []
            for dyn_id, pub_time, content, comment_count, repost_count in chunk:
                # 外库动态不含作者，统一随机映射到自有用户系统的一个用户作为 author
                mid = rng.choice(author_pool)
                topic_id = rng.choice(topic_ids) if topic_ids and rng.random() < TOPIC_LINK_RATIO else None
                audit = _audit_status()
                is_top = 1 if rng.random() < TOP_RATIO and audit is MomentAuditStatusEnum.NORMAL else 0
                moment_rows.append(
                    {
                        "dynId": dyn_id,
                        "mid": mid,
                        "dynType": MomentTypeEnum.WORD.value,
                        "contentText": content,
                        "contentJson": [{"type": "WORDS", "text": content}],
                        "topicId": topic_id,
                        "visibleScope": MomentVisibleScopeEnum.PUBLIC.value,
                        "foldType": MomentFoldTypeEnum.NONE.value,
                        "auditStatus": audit.value,
                        "pubTime": pub_time,
                        "isTop": is_top,
                        "topTime": pub_time if is_top else None,
                    }
                )
                like_target = _sample(LIKE_DISTRIBUTION)
                view_target = _sample(VIEW_DISTRIBUTION)
                stat_rows.append(
                    {
                        "dynId": dyn_id,
                        "commentCount": comment_count,
                        "repostCount": repost_count,
                        "likeCount": like_target,
                        "viewCount": view_target,
                    }
                )
                like_plan.append((dyn_id, like_target))
                view_plan.append((dyn_id, view_target))
                # 审核流水：每条动态 1 条 create 日志（normal 追加 1 条 approve）
                audit_logs.append(
                    {
                        "dynId": dyn_id,
                        "operatorMid": mid,
                        "operatorRole": MomentAuditLogOperatorRoleEnum.AUTHOR.value,
                        "fromStatus": None,
                        "toStatus": MomentAuditStatusEnum.AUDITING.value,
                        "actionType": MomentAuditLogActionEnum.CREATE.value,
                    }
                )
            # SQLModel 批量插入：mysql_insert(...).values(dict列表) + session.exec
            await session.exec(
                mysql_insert(TMoment.__table__).values(moment_rows)  # type: ignore[call-overload]
            )
            await session.exec(
                mysql_insert(TMomentStat.__table__).values(stat_rows)  # type: ignore[call-overload]
            )
            await session.commit()
            total_main += len(moment_rows)
            logger.info(f"  TMoment + TMomentStat 已插入 {total_main} / {len(reals)}")

        # 3.3 点赞明细（与 likeCount 对齐；唯一约束 dynId+mid 幂等）
        logger.info("灌入 TMomentLike…")
        likers_pool = rng.sample(author_pool, k=min(args.liker_pool_size, len(author_pool)))
        like_rows = 0
        batch_likes: list[dict] = []
        for dyn_id, like_target in like_plan:
            if like_target <= 0:
                continue
            for mid in rng.sample(likers_pool, k=min(like_target, len(likers_pool))):
                batch_likes.append({"dynId": dyn_id, "mid": mid})
                if len(batch_likes) >= args.batch_size:
                    await session.exec(
                        mysql_insert(TMomentLike.__table__).values(batch_likes)  # type: ignore[call-overload]
                    )
                    await session.commit()
                    like_rows += len(batch_likes)
                    batch_likes.clear()
        if batch_likes:
            await session.exec(
                mysql_insert(TMomentLike.__table__).values(batch_likes)  # type: ignore[call-overload]
            )
            await session.commit()
            like_rows += len(batch_likes)
        logger.info(f"  TMomentLike {like_rows} 条")

        # 3.4 浏览明细（与 viewCount 对齐；唯一约束 dynId+mid+refDate 幂等）
        logger.info("灌入 TMomentViewLog…")
        viewers_pool = rng.sample(author_pool, k=min(args.viewer_pool_size, len(author_pool)))
        view_rows = 0
        batch_views: list[dict] = []
        ref_date_by_dyn: dict[int, str] = {}
        for dyn_id, pub_time, *_rest in reals:
            ref_date_by_dyn[dyn_id] = (pub_time or datetime.datetime.now()).strftime("%Y-%m-%d")
        for dyn_id, view_target in view_plan:
            if view_target <= 0:
                continue
            ref_date = ref_date_by_dyn.get(dyn_id, datetime.datetime.now().strftime("%Y-%m-%d"))
            for mid in rng.sample(viewers_pool, k=min(view_target, len(viewers_pool))):
                batch_views.append({"dynId": dyn_id, "mid": mid, "refDate": ref_date})
                if len(batch_views) >= args.batch_size:
                    await session.exec(
                        mysql_insert(TMomentViewLog.__table__).values(batch_views)  # type: ignore[call-overload]
                    )
                    await session.commit()
                    view_rows += len(batch_views)
                    batch_views.clear()
        if batch_views:
            await session.exec(
                mysql_insert(TMomentViewLog.__table__).values(batch_views)  # type: ignore[call-overload]
            )
            await session.commit()
            view_rows += len(batch_views)
        logger.info(f"  TMomentViewLog {view_rows} 条")

        # 3.5 审核流水（SQLModel 批量）
        logger.info("灌入 TMomentAuditLog…")
        for i in range(0, len(audit_logs), args.batch_size):
            await session.exec(
                mysql_insert(TMomentAuditLog.__table__).values(
                    audit_logs[i : i + args.batch_size]
                )  # type: ignore[call-overload]
            )
            await session.commit()
        logger.info(f"  TMomentAuditLog {len(audit_logs)} 条")

        # 3.6 话题 dynCount 回填（仅 normal 未软删；对账 / 广场排序用）
        logger.info("回填 TMomentTopic.dynCount…")
        if topic_ids:
            conn = await session.connection()
            await conn.execute(
                text(
                    "UPDATE TMomentTopic t SET dynCount = ("
                    "  SELECT COUNT(*) FROM TMoment m "
                    "  WHERE m.topicId = t.topicId AND m.auditStatus = 'normal' AND m.deletedAt IS NULL"
                    ")"
                )
            )
            await session.commit()

    logger.info("灌数完成！可运行 scripts/reconcile_moment_stats.py 校验计数一致性。")


if __name__ == "__main__":
    asyncio.run(main())
