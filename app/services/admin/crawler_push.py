"""管理端：be-crawler 爬虫推送开关代理服务。

职责只有三件事：

1. **读**：代理 crawler `GET .../CrawlerPushSwitch`，把内部结构归一成本服务响应模型；
2. **写**：按 crawler_name 覆盖写入 / 删除单个爬虫配置，写入前做参数校验；
3. **审计**：写操作打一条带操作人 mid 的 info 日志（与其它管理端写接口口径一致）。

为什么不做缓存：开关状态存在 crawler 的本地 JSON 文件里，进程内缓存只会让管理端
看到旧值；请求量极小（一次最多几十行），实时转发即可。

与 `crawler_llm.py` 的差异：这里没有敏感字段（token 之类），因此读写用同一套模型，
不需要脱敏回填。
"""

from loguru import logger

from app.models.schemas.crawler_push import (
    CrawlerPushDeleteReq,
    CrawlerPushEffectiveItem,
    CrawlerPushItem,
    CrawlerPushSaveReq,
    CrawlerPushState,
    CrawlerPushStateResp,
)
from app.services.infrastructure.crawler_push import (
    CrawlerPushClient,
    CrawlerPushStateSnapshot,
    get_crawler_push_client,
)

__all__ = ["CrawlerPushService", "CrawlerPushValueError"]


class CrawlerPushValueError(ValueError):
    """入参非法（接口层映射为 400 / `INVALID_PARAM`）。"""


class CrawlerPushService:
    """crawler 推送开关的管理端编排（只做代理 + 校验 + 归一）。"""

    @staticmethod
    def _client() -> CrawlerPushClient:
        return get_crawler_push_client()

    @staticmethod
    def _to_resp(snapshot: CrawlerPushStateSnapshot) -> CrawlerPushStateResp:
        return CrawlerPushStateResp(
            state=CrawlerPushState(
                items=[
                    CrawlerPushItem(
                        crawler_name=item.crawler_name,
                        start=bool(item.start),
                        end=bool(item.end),
                    )
                    for item in snapshot.state.items
                ],
                updated_at=snapshot.state.updated_at,
            ),
            env_start=snapshot.env_start,
            env_end=snapshot.env_end,
            state_file=snapshot.state_file,
            effective=[
                CrawlerPushEffectiveItem(
                    crawler_name=item.crawler_name,
                    start=bool(item.start),
                    end=bool(item.end),
                    source=item.source,
                )
                for item in snapshot.effective
            ],
        )

    @staticmethod
    async def get_state() -> CrawlerPushStateResp:
        """读取文件内容 + 环境变量 + 每个爬虫的生效状态"""
        snapshot = await CrawlerPushService._client().get_state()
        return CrawlerPushService._to_resp(snapshot)

    @staticmethod
    def _validate_items(items: list[CrawlerPushItem]) -> list[CrawlerPushItem]:
        """去掉空名 / 重复项，保持提交顺序"""
        if not items:
            raise CrawlerPushValueError("items 不能为空")
        seen: set[str] = set()
        cleaned: list[CrawlerPushItem] = []
        for item in items:
            name = (item.crawler_name or "").strip()
            if not name:
                raise CrawlerPushValueError("crawler_name 不能为空")
            if name in seen:
                raise CrawlerPushValueError(f"crawler_name 重复：{name}")
            seen.add(name)
            cleaned.append(
                CrawlerPushItem(
                    crawler_name=name, start=bool(item.start), end=bool(item.end)
                )
            )
        return cleaned

    @staticmethod
    async def save_items(
        req: CrawlerPushSaveReq, operator_mid: int
    ) -> CrawlerPushStateResp:
        """按 crawler_name 覆盖写入（只提交改动行由调用方保证）"""
        items = CrawlerPushService._validate_items(req.items)
        await CrawlerPushService._client().set_items(items)
        logger.info(
            f"管理员 {operator_mid} 更新爬虫推送开关："
            f"{', '.join(f'{i.crawler_name}(start={i.start},end={i.end})' for i in items)}"
        )
        return await CrawlerPushService.get_state()

    @staticmethod
    async def delete_item(
        req: CrawlerPushDeleteReq, operator_mid: int
    ) -> CrawlerPushStateResp:
        """删除某个爬虫的文件配置，回退到环境变量"""
        name = (req.crawler_name or "").strip()
        if not name:
            raise CrawlerPushValueError("crawler_name 不能为空")
        await CrawlerPushService._client().delete_item(name)
        logger.info(f"管理员 {operator_mid} 恢复爬虫推送开关为环境变量：{name}")
        return await CrawlerPushService.get_state()
