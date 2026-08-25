"""动态统计服务（2.36.0 起计数统一并入 `TInteractionStat`，消除 TMomentStat 双轨）。

统一「**明细表唯一约束幂等 + 计数原子增减**」范式（计划书 §4.2）：

- 动态资源计数（like/comment/repost/view/favorite/share/dislike/coin）统一存
  `TInteractionStat`（`bizType=dynamic` 行），与 lottery/rpa_* 同一张表；
- 点赞/点踩明细 `TMomentLike`/`TMomentDislike`、浏览去重 `TInteractionViewLog`、
  评论 `CommentSubject(oid,type)` 均已泛化；`TMomentStat`/`TMomentViewLog` 废弃删除。
- 2.42.0：浏览去重明细每用户每资源一行（唯一约束 bizType+bizId+mid），
  `lastViewAt` 判自然日窗口，跨天再次访问才给 Stat.viewCount +1；
  **对账脚本已删除**（单行明细丢失逐次访问日期，无法作为对账权威源）。

本模块保留原方法签名（`incr_stat`/`decr_stat`/`incr_repost_count`/`report_view`/
`batch_read_stats`/`ensure_stat_row`），内部委托通用
`BeMessageInteractionStatService`，**调用方无需改动**。

所有计数 UPDATE 必须由调用方在**同一事务**内与明细表操作一起提交（计划书 §6.3）。
"""

from sqlmodel import col, select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.db import TInteractionStat
from app.models.enums import InteractionBizTypeEnum
from app.services.interaction import (
    BeMessageInteractionStatService as InteractionStatService,
)

#: 动态资源的 bizType 行（计数统一表）
_DYNAMIC = InteractionBizTypeEnum.DYNAMIC

# 计数列名（与 TInteractionStat 字段一致；InteractionStatService 内部有白名单校验）
_STAT_COLUMNS = frozenset(
    {
        "likeCount",
        "commentCount",
        "repostCount",
        "viewCount",
        "shareCount",
        "coinCount",
        "favoriteCount",
        "dislikeCount",
    }
)


class MomentStatService:
    """动态统计原子计数服务（静态方法集合，无状态；计数统一存 TInteractionStat）。"""

    # ==================== 原子计数（P4-T1）====================

    @staticmethod
    async def incr_stat(
        session: AsyncSession,
        moment_id: int,
        field: str,
        delta: int,
    ) -> None:
        """对指定计数列做数据库侧原子 ±delta（field ∈ _STAT_COLUMNS）。"""
        if field not in _STAT_COLUMNS:
            raise ValueError(f"不支持的计数列: {field}")
        await InteractionStatService.incr(session, _DYNAMIC, moment_id, field, delta)

    @staticmethod
    async def decr_stat(
        session: AsyncSession,
        moment_id: int,
        field: str,
        *,
        floor_zero: bool = False,
    ) -> None:
        """计数 -1（delta=-1）。floor_zero=True 时加 `>0` 兜底，避免极端负数。"""
        if field not in _STAT_COLUMNS:
            raise ValueError(f"不支持的计数列: {field}")
        await InteractionStatService.decr(
            session, _DYNAMIC, moment_id, field, floor_zero=floor_zero
        )

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
        if delta < 0:
            await InteractionStatService.decr(
                session, _DYNAMIC, src_moment_id, "repostCount", floor_zero=True
            )
        else:
            await InteractionStatService.incr(
                session, _DYNAMIC, src_moment_id, "repostCount", delta
            )

    # ==================== 浏览上报去重（P4-T4）====================

    @staticmethod
    async def report_view(
        session: AsyncSession,
        moment_id: int,
        mid: int,
    ) -> bool:
        """上报浏览量（2.42.0 每用户每资源一行，跨自然日再次访问才 +1，走 TInteractionViewLog）。

        Returns:
            True 表示新计一次浏览量（Stat.viewCount +1）；
            False 表示同日重复访问，仅 ViewLog.viewCount 自增 + 刷新 lastViewAt，不累加 Stat。
        """
        return await InteractionStatService.report_view(session, _DYNAMIC, moment_id, mid)

    # ==================== 批量读取（P4-T1）====================

    @staticmethod
    async def batch_read_stats(
        session: AsyncSession, moment_ids: list[int]
    ) -> dict[int, TInteractionStat]:
        """批量读取统计（直接读字段值，严禁 COUNT 聚合；返回 {bizId: TInteractionStat}）。"""
        if not moment_ids:
            return {}
        rows = (
            await session.exec(
                select(TInteractionStat).where(
                    col(TInteractionStat.bizType) == _DYNAMIC,
                    col(TInteractionStat.bizId).in_(moment_ids),
                )
            )
        ).all()
        return {r.bizId: r for r in rows}

    @staticmethod
    async def ensure_stat_row(session: AsyncSession, moment_id: int) -> None:
        """确保动态存在对应的计数行（TInteractionStat 惰性建行兜底）。"""
        row = (
            await session.exec(
                select(TInteractionStat).where(
                    col(TInteractionStat.bizType) == _DYNAMIC,
                    col(TInteractionStat.bizId) == moment_id,
                )
            )
        ).one_or_none()
        if row is None:
            session.add(TInteractionStat(bizType=_DYNAMIC, bizId=moment_id))
            await session.flush()

__all__ = ["_STAT_COLUMNS", "MomentStatService"]
