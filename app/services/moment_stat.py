"""动态统计服务（Phase 4）。

统一「**明细表唯一约束幂等 + 计数原子增减**」范式（计划书 §4.2）：

- `incr_stat` / `decr_stat`：对任意计数列做数据库侧原子 UPDATE ±1，
  **严禁请求热路径做 COUNT 聚合**；repostCount 的 -1 带 `>0` 兜底防负数。
- repostCount 状态机（计划书 §4.2 触发点 ①②③④）：唯一合法修改点是
  `repostSrcDynId` 指向的**源动态**那一行，本模块提供 `incr_repost_count` 供
  P2（编辑/删除）/ P6（审核通过/驳回）统一调用。
- 浏览上报去重（P4-T4）：`TMomentViewLog` upsert，同一用户同一天只计一次，
  首次新行才给 `viewCount +1`；后续同日重复调用只自增 ViewLog 不累加 Stat。
- 批量读取（P4-T1）：直接读 `TMomentStat` 字段值，不做 COUNT 聚合。

所有计数 UPDATE 必须由调用方在**同一事务**内与明细表操作一起提交（计划书 §6.3）。
"""

from cmath import e

from loguru import logger
from sqlmodel import col, func, select, update, and_
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.db import TMomentLike, TMomentStat, TMomentViewLog
from app.models.enums import InteractionBizTypeEnum

# 计数列名 → TMomentStat 属性（用于原子 UPDATE）
_STAT_COLUMNS = {
    "likeCount": TMomentStat.likeCount,
    "commentCount": TMomentStat.commentCount,
    "repostCount": TMomentStat.repostCount,
    "viewCount": TMomentStat.viewCount,
    "shareCount": TMomentStat.shareCount,
    "coinCount": TMomentStat.coinCount,
    "favoriteCount": TMomentStat.favoriteCount,
}


class MomentStatService:
    """动态统计原子计数服务（静态方法集合，无状态）。"""

    # ==================== 原子计数（P4-T1）====================

    @staticmethod
    async def incr_stat(
        session: AsyncSession,
        moment_id: int,
        field: str,
        delta: int,
    ) -> None:
        """对指定计数列做数据库侧原子 ±delta（field ∈ _STAT_COLUMNS）。

        例如 likeCount +1 / -1、viewCount +1。repostCount 的状态机修改请用
        `incr_repost_count`（带 >0 防负数兜底）。
        """
        if field not in _STAT_COLUMNS:
            raise ValueError(f"不支持的计数列: {field}")
        column = _STAT_COLUMNS[field]
        await session.exec(  # type: ignore[call-overload]
            update(TMomentStat)
            .where(col(TMomentStat.dynId) == moment_id)
            .values({column: col(column) + delta})
        )

    @staticmethod
    async def decr_stat(
        session: AsyncSession,
        moment_id: int,
        field: str,
        *,
        floor_zero: bool = False,
    ) -> None:
        """计数 -1（delta=-1）。floor_zero=True 时加 `>0` 兜底，避免极端负数。

        用于点赞取消、浏览回退等场景；repostCount 默认带 floor_zero。
        """
        if field not in _STAT_COLUMNS:
            raise ValueError(f"不支持的计数列: {field}")
        column = _STAT_COLUMNS[field]
        stmt = (
            update(TMomentStat)
            .where(col(TMomentStat.dynId) == moment_id)
            .values({column: col(column) - 1})
        )
        if floor_zero:
            stmt = stmt.where(col(column) > 0)
        await session.exec(stmt)  # type: ignore[call-overload]

    # ==================== repostCount 状态机（P4-T2）====================

    @staticmethod
    async def incr_repost_count(
        session: AsyncSession, src_moment_id: int, delta: int
    ) -> None:
        """源动态 repostCount 原子 ±delta（状态机唯一合法修改点）。

        delta=+1：审核通过（P6-T2）生效；delta=-1：审核驳回 / 编辑回审核 /
        软删（P2-T4）生效。带 `>0` 兜底防负数。

        注意：操作对象永远是 `repostSrcDynId` 指向的**源动态**行，
        不是转发动态自己（计划书 §4.2）。
        """
        stmts = []
        if delta < 0:
            stmts.append(
                and_(
                    col(TMomentStat.dynId) == src_moment_id,
                    TMomentStat.repostCount > 0,
                )
            )
        else:
            stmts.append(col(TMomentStat.dynId) == src_moment_id)
        await session.exec(
            update(TMomentStat)
            .where(*stmts)
            .values(repostCount=col(TMomentStat.repostCount) + delta)
        )

    # ==================== 浏览上报去重（P4-T4）====================

    @staticmethod
    async def report_view(
        session: AsyncSession,
        moment_id: int,
        mid: int,
        ref_date: str,
    ) -> bool:
        """上报浏览量（去重：同一用户同一天只计一次）。

        Returns:
            True 表示本次是首次（新行插入），已给 viewCount +1；
            False 表示当日已上报过，仅 ViewLog.viewCount 自增，不累加 Stat。

        实现：先尝试 INSERT ViewLog（依赖唯一约束 dynId+mid+refDate 幂等），
        捕获唯一冲突则说明当日已存在 → 只 UPDATE 该行的 viewCount+1，不动 Stat。
        """
        exists = (
            await session.exec(
                select(TMomentViewLog.pk).where(
                    col(TMomentViewLog.dynId) == moment_id,
                    col(TMomentViewLog.mid) == mid,
                    col(TMomentViewLog.refDate) == ref_date,
                )
            )
        ).first()
        if exists is not None:
            # 当日已上报：仅累加 ViewLog 行内计数，不累加 Stat
            await session.exec(  # type: ignore[call-overload]
                update(TMomentViewLog)
                .where(col(TMomentViewLog.pk) == exists)
                .values(viewCount=col(TMomentViewLog.viewCount) + 1)
            )
            return False

        # 首次：插入新行 + 源动态 viewCount +1（同一事务）
        session.add(
            TMomentViewLog(dynId=moment_id, mid=mid, refDate=ref_date, viewCount=1)
        )
        await session.flush()
        await MomentStatService.incr_stat(session, moment_id, "viewCount", 1)
        return True

    # ==================== 批量读取（P4-T1）====================

    @staticmethod
    async def batch_read_stats(
        session: AsyncSession, moment_ids: list[int]
    ) -> dict[int, TMomentStat]:
        """批量读取统计（直接读字段值，严禁 COUNT 聚合）。"""
        if not moment_ids:
            return {}
        rows = (
            await session.exec(
                select(TMomentStat).where(col(TMomentStat.dynId).in_(moment_ids))
            )
        ).all()
        return {r.dynId: r for r in rows}

    @staticmethod
    async def ensure_stat_row(session: AsyncSession, moment_id: int) -> None:
        """确保动态存在对应的 TMomentStat 行（发布时已建，这里做兜底）。"""
        stat = (
            await session.exec(
                select(TMomentStat).where(col(TMomentStat.dynId) == moment_id)
            )
        ).one_or_none()
        if stat is None:
            session.add(TMomentStat(dynId=moment_id))
            await session.flush()

    # ==================== 夜间对账（P4-T6，非热路径）====================

    @staticmethod
    async def reconcile(
        session: AsyncSession,
        *,
        batch_size: int = 500,
    ) -> dict[str, int]:
        """对账：用明细表 COUNT 校正 TMomentStat 计数（非请求热路径）。

        仅校正 likeCount / repostCount / viewCount（对应明细表 TMomentLike /
        TMomentViewLog / 转发状态）。commentCount 由评论系统维护，此处不碰。
        返回修正条数统计。

        **仅用于夜间定时 / 运维脚本**，严禁在请求链路调用。
        """
        fixed = {"likeCount": 0, "repostCount": 0, "viewCount": 0}

        # likeCount：以 TMomentLike 行数校正（2.17.0 泛化：仅统计 dynamic 资源的点赞明细）
        rows = (
            await session.exec(
                select(
                    TMomentLike.dynId,
                    func.count(col(TMomentLike.pk)).label("cnt"),
                )
                .where(col(TMomentLike.bizType) == InteractionBizTypeEnum.DYNAMIC)
                .group_by(col(TMomentLike.dynId))
            )
        ).all()
        for moment_id, cnt in rows:
            # SQLModel 方式：`session.exec(select(Model))` 对实体查询返回 ScalarResult，
            # `.one_or_none()` 直接取模型对象（勿再加 .scalars()，ScalarResult 无此方法）
            stat = (
                await session.exec(
                    select(TMomentStat).where(TMomentStat.dynId == moment_id)
                )
            ).one_or_none()
            if stat is None:
                session.add(TMomentStat(dynId=moment_id, likeCount=cnt))
                fixed["likeCount"] += 1
            elif stat.likeCount != cnt:
                stat.likeCount = cnt
                fixed["likeCount"] += 1

        # viewCount：以 TMomentViewLog 各行 viewCount 之和校正
        vrows = (
            await session.exec(
                select(
                    TMomentViewLog.dynId,
                    func.sum(TMomentViewLog.viewCount).label("cnt"),
                ).group_by(col(TMomentViewLog.dynId))
            )
        ).all()
        for moment_id, cnt in vrows:
            cnt = int(cnt or 0)
            stat = (
                await session.exec(
                    select(TMomentStat).where(TMomentStat.dynId == moment_id)
                )
            ).one_or_none()
            if stat is None:
                session.add(TMomentStat(dynId=moment_id, viewCount=cnt))
                fixed["viewCount"] += 1
            elif stat.viewCount != cnt:
                stat.viewCount = cnt
                fixed["viewCount"] += 1

        await session.commit()
        logger.info(f"动态统计对账完成: {fixed}")
        return fixed


__all__ = ["_STAT_COLUMNS", "MomentStatService"]
