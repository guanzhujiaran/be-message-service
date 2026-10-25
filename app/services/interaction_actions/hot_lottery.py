"""热门抽奖榜单（基于 be-message 浏览记录，2.64.0）。

口径：统计窗口（默认近 7 天）内 ``TInteractionViewLog``（bizType=lottery）
按资源聚合 ``sum(viewCount)`` 降序取 topN，再附 ``TInteractionStat`` 的累计
浏览 / 点赞 / 收藏与评论系统实时评论数。

抽奖本体在 be-bilibili-crawler，本服务**不存也不查抽奖内容**，只产出
「热度榜 id + 计数」；前端按 ``bizId`` 回捞抽奖详情渲染。
"""

from datetime import datetime, timedelta

from bili_common.models import InteractionBizTypeEnum
from sqlmodel import col, func, select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.db import CommentSubject, TInteractionStat, TInteractionViewLog
from app.models.schemas.interaction import HotLotteryItem
from app.services.moment.interaction import (
    BeMessageInteractionStatService as InteractionStatService,
)

__all__ = ["HotLotteryService"]


class HotLotteryService:
    """热门抽奖榜（纯服务层，不含 HTTP 语义）。"""

    @staticmethod
    async def list_hot_lotteries(
        session: AsyncSession, *, window_days: int, limit: int
    ) -> list[HotLotteryItem]:
        """按窗口内浏览热度返回热门抽奖 id 列表（降序）。

        Args:
            session: 异步会话。
            window_days: 浏览记录统计窗口（天）。
            limit: 返回条数上限。

        Returns:
            热门抽奖榜单项（可能为空列表：窗口内无任何抽奖浏览记录）。
        """
        # 时间边界按服务端本地时区计算，与库内 TIMESTAMP 直接比较（与汇总类接口一致）
        since = datetime.now() - timedelta(days=window_days)

        # 1) 窗口内浏览记录聚合：每资源 sum(viewCount) 降序取 topN
        recent_rows = (
            await session.exec(
                select(
                    col(TInteractionViewLog.bizId),
                    func.sum(col(TInteractionViewLog.viewCount)),
                )
                .where(
                    col(TInteractionViewLog.bizType) == InteractionBizTypeEnum.LOTTERY,
                    col(TInteractionViewLog.lastViewAt) >= since,
                )
                .group_by(col(TInteractionViewLog.bizId))
                .order_by(func.sum(col(TInteractionViewLog.viewCount)).desc())
                .limit(limit)
            )
        ).all()
        if not recent_rows:
            return []

        recent_view: dict[int, int] = {
            int(biz_id): int(count) for biz_id, count in recent_rows
        }
        biz_ids = list(recent_view)

        # 2) 累计计数（浏览 / 点赞 / 收藏，TInteractionStat 统一计数表）
        counts = await InteractionStatService.batch_get_counts(
            session, InteractionBizTypeEnum.LOTTERY, biz_ids
        )

        # 3) 评论数：评论系统实时计数（与互动态装配同源，oid=bizId）
        subjects = (
            await session.exec(
                select(CommentSubject).where(
                    col(CommentSubject.type) == InteractionBizTypeEnum.LOTTERY,
                    col(CommentSubject.oid).in_(biz_ids),
                )
            )
        ).all()
        comment_counts = {int(s.oid): int(s.all_count) for s in subjects}

        return [
            HotLotteryItem(
                bizId=str(biz_id),
                recentViewCount=recent_view[biz_id],
                viewCount=counts.get(biz_id, {}).get("viewCount", 0),
                likeCount=counts.get(biz_id, {}).get("likeCount", 0),
                favoriteCount=counts.get(biz_id, {}).get("favoriteCount", 0),
                commentCount=comment_counts.get(biz_id, 0),
            )
            for biz_id in biz_ids
        ]
