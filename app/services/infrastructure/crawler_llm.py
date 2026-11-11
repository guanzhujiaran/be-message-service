"""be-crawler 云端 LLM 配置 / 统计客户端（管理端代理用，2.7x）。

为什么由本服务代理而不是前端直连 crawler：

- crawler 的 `/llm/*` **没有任何鉴权**，只靠「网关不转发」实现不对公网开放
  （`controller/common/CommonRouter.py`），前端经网关访问不到；
- 本服务已有 `AdminUser` 鉴权与管理端审计日志，是最合适的代理层。

配置持久化在 crawler 本地文件 `runtime/llm_apis.json`（事实来源：文件 > `.env`），
因此这里不做任何缓存：每次请求都实时读写，避免管理端看到陈旧配置。
"""

import httpx
from loguru import logger
from pydantic import BaseModel, ConfigDict, Field

from app.core.config import settings

__all__ = [
    "CrawlerLlmClient",
    "CrawlerLlmConfig",
    "CrawlerLlmError",
    "CrawlerLlmModelsRow",
    "CrawlerLlmProbeRow",
    "CrawlerLlmStatsRow",
    "get_crawler_llm_client",
]

#: crawler 云端 LLM 配置路由（整体读取 / 整体覆盖）
_CONFIG_PATH = "/llm/config"
#: crawler 云端 LLM 运行统计路由
_STATS_PATH = "/llm/stats"
#: 撤销人工保存：删除持久化文件并热切回 `.env`（crawler 内部接口）
_OVERRIDE_PATH = "/llm/config/override"
#: 单实例连通性测试（crawler 用裸 ChatOpenAI 真实问一句 1+1=?，内部接口）
_TEST_PATH = "/llm/config/test"
#: 拉取上游 `/models` 模型列表（crawler 用 httpx 直连，内部接口）
_MODELS_PATH = "/llm/models"


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


class CrawlerLlmProbeRow(BaseModel):
    """crawler `/llm/config/test` 的结果行（诊断口径，不含 token）。"""

    model_config = ConfigDict(extra="ignore")

    ok: bool = False
    base_url: str = ""
    model_name: str = ""
    status_code: int | None = None
    latency_ms: int = 0
    error: str | None = None


class CrawlerLlmModelsRow(BaseModel):
    """crawler `/llm/models` 的结果行（诊断口径，不含 token）。"""

    model_config = ConfigDict(extra="ignore")

    ok: bool = False
    base_url: str = ""
    status_code: int | None = None
    latency_ms: int = 0
    models: list[str] = Field(default_factory=list)
    error: str | None = None


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
        timeout: float | None = None,
    ) -> object:
        """发请求并返回解析后的 JSON；任何失败统一抛 :class:`CrawlerLlmError`。

        ``timeout`` 单独给「测试」用：探测要等上游补一次 completion，比常规写配置慢得多。
        """
        budget = float(timeout if timeout is not None else self._timeout)
        try:
            async with httpx.AsyncClient(
                base_url=self._base_url, timeout=budget
            ) as client:
                resp = await client.request(method, path, json=json_body)
        except httpx.HTTPError as e:
            # httpx 的超时异常 str() 是空串，这里补上类型名，避免前端只看到「不可达：」
            reason = str(e) or e.__class__.__name__
            logger.warning(f"crawler LLM 接口不可达（{method} {path}）：{reason}")
            raise CrawlerLlmError(f"crawler 不可达：{reason}") from e

        if resp.status_code >= 400:
            # 上游的 4xx JSON 常带「为什么被拒」的关键 message，200 字符会切掉它；
            # 与 crawler 侧 probe/models 的 _ERROR_MAX_CHARS 保持同一量级
            detail = (resp.text or "").strip()[:1000]
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

    async def test_config(
        self,
        *,
        index: int | None,
        base_url: str,
        model_name: str,
        token: str,
    ) -> CrawlerLlmProbeRow:
        """对单条实例做一次真实 LLM 调用探测（不重试、不影响 crawler 状态机）。"""
        data = await self._request(
            "POST",
            _TEST_PATH,
            json_body={
                "index": index,
                "base_url": base_url,
                "model_name": model_name,
                "token": token,
            },
            # 单实例探测要等上游返回，用更长的独立超时（默认 60s）
            timeout=float(settings.crawler_llm_test_timeout),
        )
        if not isinstance(data, dict):
            raise CrawlerLlmError("crawler 探测响应格式非法（期望对象）")
        return CrawlerLlmProbeRow.model_validate(data)

    async def list_models(
        self,
        *,
        index: int | None,
        base_url: str,
        model_name: str,
        token: str,
    ) -> CrawlerLlmModelsRow:
        """拉取某条实例对应的上游模型列表（不重试、不影响 crawler 状态机）。"""
        data = await self._request(
            "POST",
            _MODELS_PATH,
            json_body={
                "index": index,
                "base_url": base_url,
                "model_name": model_name,
                "token": token,
            },
            # crawler 侧对上游 `/models` 有自己的超时（默认 20s），本层必须更大
            timeout=float(settings.crawler_llm_models_timeout),
        )
        if not isinstance(data, dict):
            raise CrawlerLlmError("crawler 模型列表响应格式非法（期望对象）")
        return CrawlerLlmModelsRow.model_validate(data)

    async def reset_configs(self) -> None:
        """撤销人工保存：删持久化文件并热切回 `.env`（幂等，文件不存在也算成功）。

        crawler 会在同一次调用里重建实例池，返回切换后的脱敏列表（本层不解析，
        由服务层重新 `get_configs()` 统一取数，避免两处口径）。
        """
        await self._request("DELETE", _OVERRIDE_PATH)


#: 进程内单例（base_url / timeout 在启动时从 settings 读取）
_client = CrawlerLlmClient()


def get_crawler_llm_client() -> CrawlerLlmClient:
    """取 crawler LLM 客户端单例（供服务层 / 测试替换）。"""
    return _client
