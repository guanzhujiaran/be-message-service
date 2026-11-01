"""热门抽奖榜单（基于 be-message 浏览记录，2.64.0；2.67.0 一次返回卡片数据）。

口径：统计窗口（默认近 7 天）内 ``TInteractionViewLog``（bizType=lottery）
按资源聚合 ``sum(viewCount)`` 降序取 topN，再附 ``TInteractionStat`` 的累计
浏览 / 点赞 / 收藏与评论系统实时评论数。

抽奖本体在 be-bilibili-crawler，本服务**不落库抽奖内容**，但装配榜单时经
crawler 只读查询服务（``GetLotteryDetailList``）一次批量回捞卡片（raw + extra_info），使
``GET /interaction/hot-lotteries`` 一次返回「热度 + 卡片」，前端首页不再
二次调用 detail 域接口（2.67.0）。卡片缺失（查询失败 / 已删除）的项直接剔除。
"""

from datetime import datetime, timedelta

from bili_common.models import InteractionBizTypeEnum
from sqlmodel import col, func, select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.db import CommentSubject, TInteractionStat, TInteractionViewLog
from app.models.schemas.interaction import HotLotteryItem
from app.services.infrastructure.crawler_query import get_crawler_query_client
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
            热门抽奖榜单项（可能为空列表：窗口内无任何抽奖浏览记录，或卡片全部取不到）。
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

        # 4) 卡片内容：一次批量向 crawler 只读查询服务取数（2.67.0；弱依赖 —— 取不到
        #    卡片的项直接剔除，前端因此无需再调 detail 域接口，也不会渲染空壳卡片）
        client = get_crawler_query_client()
        cards = await client.get_lottery_cards(biz_ids)

        items: list[HotLotteryItem] = []
        for biz_id in biz_ids:
            card = cards.value_of(biz_id)
            if card is None:
                continue
            items.append(
                HotLotteryItem(
                    bizId=str(biz_id),
                    recentViewCount=recent_view[biz_id],
                    viewCount=counts.get(biz_id, {}).get("viewCount", 0),
                    likeCount=counts.get(biz_id, {}).get("likeCount", 0),
                    favoriteCount=counts.get(biz_id, {}).get("favoriteCount", 0),
                    commentCount=comment_counts.get(biz_id, 0),
                    card=card,
                )
            )
        return items
