"""be-crawler 爬虫推送开关客户端（管理端代理用）。

为什么由本服务代理而不是前端直连 crawler：

- crawler 的 `/api/v1/background_service/*` **没有任何鉴权**，只靠「网关不转发」
  实现不对公网开放（crawler 侧注释见 `controller/common/CommonRouter.py`）；
- 本服务已有 `AdminUser` 鉴权与管理端审计日志，是最合适的代理层。

与 `crawler_llm.py` 同一套模式：不做缓存，每次请求实时转发——开关状态本来就存在
crawler 的本地 JSON 文件里，缓存只会让管理端看到旧值。
"""

import httpx
from loguru import logger
from pydantic import BaseModel, ConfigDict, Field

from app.core.config import settings

__all__ = [
    "CrawlerPushClient",
    "CrawlerPushError",
    "CrawlerPushItemRow",
    "CrawlerPushStateRow",
    "CrawlerPushStateSnapshot",
    "get_crawler_push_client",
]

#: crawler 推送开关路由（查询 / 覆盖写入 / 删除单个爬虫配置）
_STATE_PATH = "/api/v1/background_service/BackgroundService/CrawlerPushSwitch"
_SET_PATH = f"{_STATE_PATH}/Set"
_DELETE_PATH = f"{_STATE_PATH}/Delete"


class CrawlerPushError(RuntimeError):
    """crawler 推送开关接口调用失败（不可达 / 非 2xx / 业务 code 非 0）。"""


class CrawlerPushItemRow(BaseModel):
    """crawler 返回的单个爬虫开关"""

    crawler_name: str
    start: bool = False
    end: bool = False


class CrawlerPushStateRow(BaseModel):
    """crawler 文件里持久化的内容"""

    model_config = ConfigDict(extra="ignore")

    items: list[CrawlerPushItemRow] = Field(default_factory=list)
    updated_at: int | None = None


class CrawlerPushEffectiveRow(CrawlerPushItemRow):
    """合并「文件 + 环境变量」之后的生效状态"""

    source: str = "env"


class CrawlerPushStateSnapshot(BaseModel):
    """`GET .../CrawlerPushSwitch` 的 data 快照"""

    model_config = ConfigDict(extra="ignore")

    state: CrawlerPushStateRow = Field(default_factory=CrawlerPushStateRow)
    env_start: str = ""
    env_end: str = ""
    state_file: str = ""
    effective: list[CrawlerPushEffectiveRow] = Field(default_factory=list)


class CrawlerPushClient:
    """crawler 写服务（默认 23333）的推送开关 HTTP 客户端。"""

    def __init__(self) -> None:
        self._base_url = settings.crawler_base_url.rstrip("/")
        self._timeout = float(settings.crawler_admin_timeout)

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: object | None = None,
        params: dict[str, str] | None = None,
    ) -> object:
        """发请求并返回 crawler 的业务 data；任何失败统一抛 :class:`CrawlerPushError`。"""
        try:
            async with httpx.AsyncClient(
                base_url=self._base_url, timeout=self._timeout
            ) as client:
                resp = await client.request(
                    method, path, json=json_body, params=params
                )
        except httpx.HTTPError as e:
            logger.warning(f"crawler 推送开关接口不可达（{method} {path}）：{e}")
            raise CrawlerPushError(f"crawler 不可达：{e}") from e

        if resp.status_code >= 400:
            detail = (resp.text or "").strip()[:200]
            logger.warning(
                f"crawler 推送开关接口返回错误（{method} {path} "
                f"status={resp.status_code}）：{detail}"
            )
            raise CrawlerPushError(f"crawler 返回 {resp.status_code}：{detail}")
        try:
            payload = resp.json()
        except ValueError as e:
            raise CrawlerPushError("crawler 返回内容不是合法 JSON") from e

        if not isinstance(payload, dict):
            raise CrawlerPushError("crawler 响应格式非法（期望对象）")
        code = payload.get("code")
        if code not in (0, "0"):
            raise CrawlerPushError(
                f"crawler 业务错误 code={code}：{payload.get('msg') or '未知错误'}"
            )
        logger.debug(f"crawler 推送开关接口调用成功（{method} {path}）")
        return payload.get("data")

    async def get_state(self) -> CrawlerPushStateSnapshot:
        """读取文件内容 + 环境变量 + 每个爬虫的生效状态"""
        data = await self._request("GET", _STATE_PATH)
        if not isinstance(data, dict):
            raise CrawlerPushError("crawler 推送开关响应格式非法（期望对象）")
        return CrawlerPushStateSnapshot.model_validate(data)

    async def set_items(self, items: list[CrawlerPushItemRow]) -> CrawlerPushStateRow:
        """按 crawler_name 覆盖写入（未列出的爬虫保持原配置）"""
        payload = {"items": [item.model_dump() for item in items]}
        data = await self._request("POST", _SET_PATH, json_body=payload)
        if not isinstance(data, dict):
            raise CrawlerPushError("crawler 推送开关响应格式非法（期望对象）")
        return CrawlerPushStateRow.model_validate(data)

    async def delete_item(self, crawler_name: str) -> CrawlerPushStateRow:
        """删除某个爬虫的文件配置，使其回退到环境变量"""
        data = await self._request(
            "POST", _DELETE_PATH, params={"crawler_name": crawler_name}
        )
        if not isinstance(data, dict):
            raise CrawlerPushError("crawler 推送开关响应格式非法（期望对象）")
        return CrawlerPushStateRow.model_validate(data)


#: 进程内单例（base_url / timeout 在启动时从 settings 读取）
_client = CrawlerPushClient()


def get_crawler_push_client() -> CrawlerPushClient:
    """取 crawler 推送开关客户端单例（供服务层 / 测试替换）。"""
    return _client
