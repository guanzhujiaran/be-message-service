"""热门抽奖榜单（2.64.0；2.67.0 一次返回卡片数据）。

支持两种口径（``rank_by``）：
- ``site``（默认）：统计窗口（默认近 7 天）内 ``TInteractionViewLog``（bizType=lottery）
  按资源统计**去重浏览人数**（2.42.0 起每用户每资源一行，``COUNT(*)`` 即人数）降序；
- ``bili``：crawler 抓取时 ``lotdata.participants`` 降序，仅统计进行中的
  官方 / 预约 / 充电抽奖，避免历史抽奖霸榜。

两种口径均再附 ``TInteractionStat`` 的累计浏览 / 点赞 / 收藏与评论系统实时评论数。
抽奖本体在 be-bilibili-crawler，本服务**不落库抽奖内容**：site 口径经
``GetLotteryDetailList`` 按热度 id 批量回捞卡片，bili 口径直接使用
``GetTopLotteryByParticipants`` 返回的卡片，使 ``GET /interaction/hot-lotteries``
一次返回「排序指标 + 卡片」，前端首页不再二次调用 detail 域接口。
卡片缺失（查询失败 / 已删除）的项直接剔除。
"""

from datetime import datetime, timedelta

from bili_common.models import InteractionBizTypeEnum
from bili_common.rpc.lottery import LotteryCard
from sqlmodel import col, func, select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.db import CommentSubject, TInteractionViewLog
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
        """按窗口内去重浏览人数返回热门抽奖 id 列表（降序）。

        Args:
            session: 异步会话。
            window_days: 浏览记录统计窗口（天）。
            limit: 返回条数上限。

        Returns:
            热门抽奖榜单项（可能为空列表：窗口内无任何抽奖浏览记录，或卡片全部取不到）。
        """
        # 时间边界按服务端本地时区计算，与库内 TIMESTAMP 直接比较（与汇总类接口一致）
        since = datetime.now() - timedelta(days=window_days)

        # 1) 窗口内浏览记录聚合：每用户每资源一行，COUNT 即窗口内去重浏览人数
        #    （lastViewAt 是最后访问时间：>= since 等价于窗口内至少访问过一次）
        recent_rows = (
            await session.exec(
                select(
                    col(TInteractionViewLog.bizId),
                    func.count(),
                )
                .where(
                    col(TInteractionViewLog.bizType) == InteractionBizTypeEnum.LOTTERY,
                    col(TInteractionViewLog.lastViewAt) >= since,
                )
                .group_by(col(TInteractionViewLog.bizId))
                .order_by(
                    func.count().desc(),
                    col(TInteractionViewLog.bizId).asc(),
                )
                .limit(limit)
            )
        ).all()
        if not recent_rows:
            return []

        recent_viewers: dict[int, int] = {
            int(biz_id): int(count) for biz_id, count in recent_rows
        }
        biz_ids = list(recent_viewers)

        # 2) 卡片内容：一次批量向 crawler 只读查询服务取数（2.67.0；弱依赖 —— 取不到
        #    卡片的项直接剔除，前端因此无需再调 detail 域接口，也不会渲染空壳卡片）
        cards_index = await get_crawler_query_client().get_lottery_cards(biz_ids)
        cards = {
            biz_id: card
            for biz_id in biz_ids
            if (card := cards_index.value_of(biz_id)) is not None
        }
        return await HotLotteryService._assemble(
            session, biz_ids, recent_viewers, cards
        )

    @staticmethod
    async def list_bili_hot_lotteries(
        session: AsyncSession, *, limit: int
    ) -> list[HotLotteryItem]:
        """按爬虫抓取时的 B 站参与人数返回进行中的热门抽奖（降序）。

        数据源为 crawler 只读查询服务的 ``GetTopLotteryByParticipants``：
        仅 status=unfinished 且未过开奖时间的官方 / 预约 / 充电抽奖，
        按 ``lotdata.participants``（抓取时快照，可能非实时）降序；
        本服务只补本站累计计数 / 评论数。
        """
        cards_list = (
            await get_crawler_query_client().get_top_lottery_cards_by_participants(
                limit
            )
        )
        if not cards_list:
            return []
        # crawler 已按 participants 排序；这里按卡片 raw 兜底排序并截断
        cards_list.sort(
            key=lambda card: (
                card.raw.participants or 0,
                int(card.raw.lottery_id),
            ),
            reverse=True,
        )
        cards = {int(card.raw.lottery_id): card for card in cards_list}
        biz_ids = list(cards)[:limit]
        return await HotLotteryService._assemble(session, biz_ids, {}, cards)

    @staticmethod
    async def _assemble(
        session: AsyncSession,
        biz_ids: list[int],
        recent_viewers: dict[int, int],
        cards: dict[int, LotteryCard],
    ) -> list[HotLotteryItem]:
        """补齐累计计数 / 评论数并装配榜单（site / bili 两种口径共用）。"""
        if not biz_ids:
            return []

        counts = await InteractionStatService.batch_get_counts(
            session, InteractionBizTypeEnum.LOTTERY, biz_ids
        )
        subjects = (
            await session.exec(
                select(CommentSubject).where(
                    col(CommentSubject.type) == InteractionBizTypeEnum.LOTTERY,
                    col(CommentSubject.oid).in_(biz_ids),
                )
            )
        ).all()
        comment_counts = {int(s.oid): int(s.all_count) for s in subjects}

        items: list[HotLotteryItem] = []
        for biz_id in biz_ids:
            card = cards.get(biz_id)
            if card is None:
                continue
            items.append(
                HotLotteryItem(
                    bizId=str(biz_id),
                    recentViewCount=recent_viewers.get(biz_id, 0),
                    viewCount=counts.get(biz_id, {}).get("viewCount", 0),
                    likeCount=counts.get(biz_id, {}).get("likeCount", 0),
                    favoriteCount=counts.get(biz_id, {}).get("favoriteCount", 0),
                    commentCount=comment_counts.get(biz_id, 0),
                    card=card,
                )
            )
        return items
