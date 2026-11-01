"""be-bilibili-crawler 只读查询服务客户端（2.67.0）。

热门抽奖榜 / 首页轮播需要「一次返回卡片数据」：be-message 在装配榜单时经
crawler 的**只读查询服务**（`query_main`，`GetLotteryDetailList`）批量取卡片
（`raw` + `extra_info`，与 `GetLotteryDetail` 的 data 同形态），使前端一次请求
即可渲染，不再各自调用 detail 域接口。

为什么走 HTTP 而不是 RPC：这里要的是「一次返回卡片数据」（榜单 / 轮播的批量取数），
只读查询服务本来就提供同形态的响应，与前端首页直连的查询服务口径完全一致，
比「发 MQ + 等 reply_to」少一跳、少一个队列消费者健康度的依赖。
（RPC 已是只读取数、服务端在 query 容器（2.70.0），取数走 RPC 同样可行，
但本场景没有理由绕到 MQ。）

弱依赖：超时 / 连接失败 / 非 0 业务码 / 解析失败一律返回空集合，调用方剔除该项
（榜单少展示），不把下游抖动放大成 500。
"""

import time

import httpx
from bili_common.rpc.lottery import LotteryCard, LotteryCardList
from loguru import logger

from app.core.config import settings
from app.services.common.indexed import Indexed

__all__ = ["CrawlerQueryClient", "get_crawler_query_client"]

#: crawler 抽奖卡片批量详情路由（只读查询服务）
_LOTTERY_DETAIL_LIST_PATH = "/api/v1/lottery_database/bili/GetLotteryDetailList"
#: 单次 lottery_ids 上限（与 crawler 侧 LOTTERY_DETAIL_BATCH_LIMIT 保持一致）
_BATCH_LIMIT = 50


class CrawlerQueryClient:
    """crawler 只读查询服务客户端（当前仅抽奖卡片批量详情）。"""

    def __init__(self) -> None:
        self._base_url = settings.crawler_query_base_url.rstrip("/")
        self._timeout = float(settings.crawler_query_timeout)

    async def get_lottery_cards(self, lottery_ids: list[int]) -> Indexed[LotteryCard]:
        """批量取抽奖卡片（弱依赖：失败返回空集合）。

        Args:
            lottery_ids: 抽奖主键（去重保序后按后端 50 上限截断）。

        Returns:
            `Indexed[LotteryCard]`（lottery_id → 卡片）；不存在的 id 不放入结果，
            调用失败 / 解析失败返回空集合。
        """
        ids = list(dict.fromkeys(int(i) for i in lottery_ids))[:_BATCH_LIMIT]
        if not ids:
            return Indexed.empty()
        try:
            async with httpx.AsyncClient(
                base_url=self._base_url, timeout=self._timeout
            ) as client:
                resp = await client.get(
                    _LOTTERY_DETAIL_LIST_PATH,
                    params={
                        "lottery_ids": ",".join(str(i) for i in ids),
                        # 与前端一致：附带时间戳防浏览器 / CDN 缓存（后端不使用该值）
                        "t": int(time.time()),
                    },
                )
                resp.raise_for_status()
                payload = resp.json()
        except Exception as e:  # noqa: BLE001 弱依赖：任何失败都降级
            logger.warning(
                f"[CrawlerQueryClient] GetLotteryDetailList 调用失败（弱依赖降级）: {e}"
            )
            return Indexed.empty()

        try:
            cards = LotteryCardList.model_validate(payload.get("data") or {})
        except Exception as e:  # noqa: BLE001 响应结构变化不应打挂榜单
            logger.warning(f"[CrawlerQueryClient] GetLotteryDetailList 响应解析失败: {e}")
            return Indexed.empty()

        return Indexed.of((int(card.raw.lottery_id), card) for card in cards.items)


#: 全局单例（base_url / timeout 在进程启动时从 settings 读取）
_client = CrawlerQueryClient()


def get_crawler_query_client() -> CrawlerQueryClient:
    """获取 crawler 只读查询客户端单例。"""
    return _client
