"""be-crawler 云端 LLM 配置 / 统计客户端（管理端代理用，2.7x）。

为什么由本服务代理而不是前端直连 crawler：

- crawler 的 `/llm/*` **没有任何鉴权**，只靠「网关不转发」实现不对公网开放
  （`controller/common/CommonRouter.py`），前端经网关访问不到；
- 本服务已有 `AdminUser` 鉴权与管理端审计日志，是最合适的代理层。

数据只存于 crawler 进程内存 / `.env`（`settings.llm_apis`），因此这里不做任何缓存：
每次请求都实时读写，避免管理端看到陈旧配置。
"""

import httpx
from loguru import logger
from pydantic import BaseModel, ConfigDict, Field

from app.core.config import settings

__all__ = [
    "CrawlerLlmClient",
    "CrawlerLlmConfig",
    "CrawlerLlmError",
    "CrawlerLlmStatsRow",
    "get_crawler_llm_client",
]

#: crawler 云端 LLM 配置路由（整体读取 / 整体覆盖）
_CONFIG_PATH = "/llm/config"
#: crawler 云端 LLM 运行统计路由
_STATS_PATH = "/llm/stats"


class CrawlerLlmError(RuntimeError):
    """crawler `/llm/*` 调用失败（不可达 / 非 2xx / 响应不可解析）。"""


class CrawlerLlmConfig(BaseModel):
    """crawler 返回的单条实例配置（字段与 `CONFIG.py::LLMApiConfig` 一致）。"""

    base_url: str = ""
    model_name: str = ""
    token: str = ""
    requests_per_second: float = 0.5


class CrawlerLlmStatsRow(BaseModel):
    """crawler `/llm/stats` 的单行（只取管理端需要的字段，其余忽略）。"""

    model_config = ConfigDict(extra="ignore")

    model: str = ""
    base_url: str = ""
    state: str = "unknown"
    available: bool = False
    disabled: bool = False
    disabled_reason: str | None = None
    invoke_count: int = 0
    success_count: int = 0
    failure_count: int = 0
    consecutive_failures: int = 0
    rate_per_minute: int = 0
    avg_latency_seconds: float = 0.0
    total_tokens: int = 0
    last_used_at: float | None = Field(default=None)
    resume_at: float | None = Field(default=None)
    last_error: str | None = None


class CrawlerLlmClient:
    """crawler 写服务（默认 23333）的 HTTP 客户端。"""

    def __init__(self) -> None:
        self._base_url = settings.crawler_base_url.rstrip("/")
        self._timeout = float(settings.crawler_admin_timeout)

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: object | None = None,
    ) -> object:
        """发请求并返回解析后的 JSON；任何失败统一抛 :class:`CrawlerLlmError`。"""
        try:
            async with httpx.AsyncClient(
                base_url=self._base_url, timeout=self._timeout
            ) as client:
                resp = await client.request(method, path, json=json_body)
        except httpx.HTTPError as e:
            logger.warning(f"crawler LLM 接口不可达（{method} {path}）：{e}")
            raise CrawlerLlmError(f"crawler 不可达：{e}") from e

        if resp.status_code >= 400:
            detail = (resp.text or "").strip()[:200]
            logger.warning(
                f"crawler LLM 接口返回错误（{method} {path} "
                f"status={resp.status_code}）：{detail}"
            )
            raise CrawlerLlmError(f"crawler 返回 {resp.status_code}：{detail}")
        try:
            return resp.json()
        except ValueError as e:
            raise CrawlerLlmError("crawler 返回内容不是合法 JSON") from e

    async def get_configs(self) -> list[CrawlerLlmConfig]:
        """读取当前全部 LLM 实例配置（token 已由 crawler 脱敏）。"""
        data = await self._request("GET", _CONFIG_PATH)
        if not isinstance(data, list):
            raise CrawlerLlmError("crawler 配置响应格式非法（期望数组）")
        return [CrawlerLlmConfig.model_validate(row) for row in data]

    async def replace_configs(
        self, configs: list[CrawlerLlmConfig]
    ) -> list[CrawlerLlmConfig]:
        """整体覆盖 LLM 实例配置（热更新；crawler 失败时自身回滚）。"""
        payload = [cfg.model_dump() for cfg in configs]
        data = await self._request("PUT", _CONFIG_PATH, json_body=payload)
        if not isinstance(data, list):
            raise CrawlerLlmError("crawler 配置响应格式非法（期望数组）")
        return [CrawlerLlmConfig.model_validate(row) for row in data]

    async def get_stats(self) -> list[CrawlerLlmStatsRow]:
        """读取各实例运行统计。"""
        data = await self._request("GET", _STATS_PATH)
        if not isinstance(data, list):
            raise CrawlerLlmError("crawler 统计响应格式非法（期望数组）")
        return [CrawlerLlmStatsRow.model_validate(row) for row in data]


#: 进程内单例（base_url / timeout 在启动时从 settings 读取）
_client = CrawlerLlmClient()


def get_crawler_llm_client() -> CrawlerLlmClient:
    """取 crawler LLM 客户端单例（供服务层 / 测试替换）。"""
    return _client
