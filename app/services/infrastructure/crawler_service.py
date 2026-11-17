"""be-crawler 后台服务（调度任务）控制客户端。

与 `crawler_push.py` 同一套模式：不做缓存，每次请求实时转发；crawler 侧接口无鉴权，
只允许本服务（内网）调用。

crawler 的任务 id 规则见 `Service/BaseCrawler/launcher/scheduler_launcher.py`：
`job_id = f"crawler_job_{crawler_name}"`，因此从调度器状态里按前缀反解服务名。
"""

import httpx
from loguru import logger
from pydantic import BaseModel, ConfigDict, Field

from app.core.config import settings

__all__ = [
    "ALLOWED_SERVICE_NAMES",
    "CrawlerScheduledRow",
    "CrawlerServiceClient",
    "CrawlerServiceError",
    "get_crawler_service_client",
]

#: crawler 全局调度器状态（读取已注册任务）
_STATUS_PATH = "/api/v1/background_service/GlobalScheduler/Status"
#: 启停 / 重启（`background_service_name` 走 query）
_ACTION_PATHS = {
    "start": "/api/v1/background_service/BackgroundService/Start",
    "stop": "/api/v1/background_service/BackgroundService/Stop",
    "restart": "/api/v1/background_service/BackgroundService/Restart",
}
#: 调度器任务 id 前缀（与 crawler `BaseScheduler.job_id` 保持一致）
_JOB_ID_PREFIX = "crawler_job_"

#: crawler `BackgroundServiceName` 全量取值（服务端枚举变更时需同步）
ALLOWED_SERVICE_NAMES = frozenset(
    {
        "DYN_DETAIL_DATABASE_CLEANER",
        "TABLESPACE_MAINTENANCE",
        "PROXY_POOL_CLEANER",
        "GET_PROXY_METHODS_SCHEDULER",
        "SAMSCCLUB_SCHEDULER",
        "SAMSCCLUB_SPU_DETAIL_SCHEDULER",
        "GET_RESERVE_INFO",
        "GET_DYN",
        "GET_TOPIC",
        "REFRESH_BILI_LOTDATA_DATABASE",
        "LOTTERY_API_ROBOT_DYN_SCHEDULER",
        "LOTTERY_API_ROBOT_RESERVE_SCHEDULER",
        "GMFLV2_SCHEDULER",
        "GET_OTHERS_LOT_DYN",
        "STUCK_CHECK_SCHEDULER",
    }
)


class CrawlerServiceError(RuntimeError):
    """crawler 后台服务接口调用失败（不可达 / 非 2xx / 业务 code 非 0）。"""


class CrawlerScheduledRow(BaseModel):
    """调度器中的一个后台服务任务"""

    model_config = ConfigDict(extra="ignore")

    service_name: str
    next_run_time: float | None = Field(default=None)


class CrawlerServiceClient:
    """crawler 写服务（默认 23333）的后台服务控制 HTTP 客户端。"""

    def __init__(self) -> None:
        self._base_url = settings.crawler_base_url.rstrip("/")
        self._timeout = float(settings.crawler_admin_timeout)

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
    ) -> dict:
        """发请求并返回 crawler 的完整响应体；任何失败统一抛 :class:`CrawlerServiceError`。"""
        try:
            async with httpx.AsyncClient(
                base_url=self._base_url, timeout=self._timeout
            ) as client:
                resp = await client.request(method, path, params=params)
        except httpx.HTTPError as e:
            logger.warning(f"crawler 后台服务接口不可达（{method} {path}）：{e}")
            raise CrawlerServiceError(f"crawler 不可达：{e}") from e

        if resp.status_code >= 400:
            detail = (resp.text or "").strip()[:200]
            logger.warning(
                f"crawler 后台服务接口返回错误（{method} {path} "
                f"status={resp.status_code}）：{detail}"
            )
            raise CrawlerServiceError(f"crawler 返回 {resp.status_code}：{detail}")
        try:
            payload = resp.json()
        except ValueError as e:
            raise CrawlerServiceError("crawler 返回内容不是合法 JSON") from e

        if not isinstance(payload, dict):
            raise CrawlerServiceError("crawler 响应格式非法（期望对象）")
        code = payload.get("code")
        if code not in (0, "0"):
            raise CrawlerServiceError(
                f"crawler 业务错误 code={code}：{payload.get('msg') or '未知错误'}"
            )
        return payload

    async def get_scheduled(self) -> list[CrawlerScheduledRow]:
        """读取全局调度器里已注册的 crawler 任务（未出现的服务即「已停止」）"""
        payload = await self._request("GET", _STATUS_PATH)
        data = payload.get("data")
        if not isinstance(data, dict):
            raise CrawlerServiceError("crawler 调度器状态响应格式非法（期望对象）")

        rows: list[CrawlerScheduledRow] = []
        for job in data.get("jobs") or []:
            if not isinstance(job, dict):
                continue
            info = job.get("job_info")
            if not isinstance(info, dict):
                continue
            job_id = str(info.get("id") or "")
            if not job_id.startswith(_JOB_ID_PREFIX):
                continue
            service_name = job_id[len(_JOB_ID_PREFIX) :]
            if not service_name:
                continue
            next_run_time = info.get("next_run_time")
            rows.append(
                CrawlerScheduledRow(
                    service_name=service_name,
                    next_run_time=float(next_run_time)
                    if next_run_time is not None
                    else None,
                )
            )
        return rows

    async def operate(self, action: str, service_name: str) -> str:
        """执行 start / stop / restart，返回 crawler 的 msg 描述"""
        path = _ACTION_PATHS.get(action)
        if path is None:
            raise CrawlerServiceError(f"不支持的动作：{action}")
        payload = await self._request(
            "POST", path, params={"background_service_name": service_name}
        )
        return str(payload.get("msg") or "")


#: 进程内单例（base_url / timeout 在启动时从 settings 读取）
_client = CrawlerServiceClient()


def get_crawler_service_client() -> CrawlerServiceClient:
    """取 crawler 后台服务客户端单例（供服务层 / 测试替换）。"""
    return _client
