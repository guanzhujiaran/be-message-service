"""管理端：be-crawler 后台服务启停代理服务。

职责只有三件事：

1. **读**：代理 crawler 全局调度器状态，归一成「已排期的服务名 + 下次触发时间」；
2. **写**：校验服务名后代理 start / stop / restart，返回 crawler 的结果描述；
3. **审计**：写操作打一条带操作人 mid 的 info 日志（与其它管理端写接口口径一致）。

不做缓存：调度状态与启停结果都以 crawler 进程内状态为准，缓存只会让页面看到旧值。
"""

from loguru import logger

from app.models.schemas.crawler_service import (
    CrawlerScheduledService,
    CrawlerServiceAction,
    CrawlerServiceOperateReq,
    CrawlerServiceOperateResp,
    CrawlerServiceStatusResp,
)
from app.services.infrastructure.crawler_service import (
    ALLOWED_SERVICE_NAMES,
    CrawlerServiceClient,
    get_crawler_service_client,
)

__all__ = ["CrawlerServiceService", "CrawlerServiceValueError"]


class CrawlerServiceValueError(ValueError):
    """入参非法（接口层映射为 400 / `INVALID_PARAM`）。"""


class CrawlerServiceService:
    """crawler 后台服务的管理端编排（只做代理 + 校验 + 归一）。"""

    @staticmethod
    def _client() -> CrawlerServiceClient:
        return get_crawler_service_client()

    @staticmethod
    async def get_status() -> CrawlerServiceStatusResp:
        """读取当前已注册在全局调度器中的后台服务"""
        rows = await CrawlerServiceService._client().get_scheduled()
        return CrawlerServiceStatusResp(
            scheduled=[
                CrawlerScheduledService(
                    service_name=row.service_name, next_run_time=row.next_run_time
                )
                for row in rows
            ]
        )

    @staticmethod
    def _validate_service_name(service_name: str) -> str:
        name = (service_name or "").strip()
        if not name:
            raise CrawlerServiceValueError("service_name 不能为空")
        if name not in ALLOWED_SERVICE_NAMES:
            raise CrawlerServiceValueError(f"未知的后台服务名：{name}")
        return name

    @staticmethod
    async def operate(
        action: CrawlerServiceAction,
        req: CrawlerServiceOperateReq,
        operator_mid: int,
    ) -> CrawlerServiceOperateResp:
        """执行启停 / 重启，并记录管理端审计日志"""
        service_name = CrawlerServiceService._validate_service_name(req.service_name)
        msg = await CrawlerServiceService._client().operate(
            action.value, service_name
        )
        logger.info(
            f"管理员 {operator_mid} 对后台服务 {service_name} 执行 {action.value}：{msg}"
        )
        return CrawlerServiceOperateResp(
            service_name=service_name, action=action, msg=msg
        )
