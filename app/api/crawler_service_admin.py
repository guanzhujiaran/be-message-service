"""管理端：be-crawler 后台爬虫服务启停（/api/v1/crawler/admin/service）。

crawler 的 `/api/v1/background_service/*` 无鉴权且不在网关转发白名单内，
本服务已有 `AdminUser` 鉴权 + 管理端审计日志，作为代理层（同推送开关 / LLM 配置的先例）。

接口清单：

- `GET  ""`        查询全局调度器里已注册的后台服务（用于判断「已停止 / 已排期」）；
- `POST /start`    启动某个后台服务；
- `POST /stop`     停止某个后台服务；
- `POST /restart`  重启某个后台服务。
"""

from bili_common.models import ResponseCode
from fastapi import APIRouter

from app.dependencies import AdminUser
from app.models import StandardResponse
from app.models.schemas.crawler_service import (
    CrawlerServiceAction,
    CrawlerServiceOperateReq,
    CrawlerServiceOperateResp,
    CrawlerServiceStatusResp,
)
from app.services.admin.crawler_service import (
    CrawlerServiceService,
    CrawlerServiceValueError,
)
from app.services.infrastructure.crawler_service import CrawlerServiceError

router = APIRouter(prefix="/api/v1/crawler/admin/service", tags=["crawler-service-admin"])


@router.get(
    "",
    response_model=StandardResponse[CrawlerServiceStatusResp],
    summary="后台爬虫服务调度状态（已注册的服务列表）",
)
async def get_service_status(
    user: AdminUser,
) -> StandardResponse[CrawlerServiceStatusResp]:
    """只返回已注册在全局调度器中的服务；未出现的服务即「已停止」。"""
    try:
        data = await CrawlerServiceService.get_status()
    except CrawlerServiceError as e:
        return StandardResponse(code=int(ResponseCode.NETWORK_ERROR), msg=str(e))
    return StandardResponse(data=data)


@router.post(
    "/start",
    response_model=StandardResponse[CrawlerServiceOperateResp],
    summary="启动指定的后台爬虫服务",
)
async def start_service(
    user: AdminUser, req: CrawlerServiceOperateReq
) -> StandardResponse[CrawlerServiceOperateResp]:
    return await _operate(CrawlerServiceAction.START, req, int(user.mid))


@router.post(
    "/stop",
    response_model=StandardResponse[CrawlerServiceOperateResp],
    summary="停止指定的后台爬虫服务",
)
async def stop_service(
    user: AdminUser, req: CrawlerServiceOperateReq
) -> StandardResponse[CrawlerServiceOperateResp]:
    return await _operate(CrawlerServiceAction.STOP, req, int(user.mid))


@router.post(
    "/restart",
    response_model=StandardResponse[CrawlerServiceOperateResp],
    summary="重启指定的后台爬虫服务",
)
async def restart_service(
    user: AdminUser, req: CrawlerServiceOperateReq
) -> StandardResponse[CrawlerServiceOperateResp]:
    return await _operate(CrawlerServiceAction.RESTART, req, int(user.mid))


async def _operate(
    action: CrawlerServiceAction, req: CrawlerServiceOperateReq, operator_mid: int
) -> StandardResponse[CrawlerServiceOperateResp]:
    """三个动作共用同一套错误映射：参数非法 → 400，crawler 调用失败 → 1003。"""
    try:
        data = await CrawlerServiceService.operate(action, req, operator_mid)
    except CrawlerServiceValueError as e:
        return StandardResponse(code=int(ResponseCode.INVALID_PARAM), msg=str(e))
    except CrawlerServiceError as e:
        return StandardResponse(code=int(ResponseCode.NETWORK_ERROR), msg=str(e))
    return StandardResponse(data=data)
