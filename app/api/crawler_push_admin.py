"""管理端：be-crawler 爬虫「开始 / 结束」推送开关（/api/v1/crawler/admin/push-switch）。

为什么放在 be-message：crawler 的 `/api/v1/background_service/*` 无鉴权且不在网关
转发白名单内（crawler 侧只靠「网关不转发」保证不对公网开放），本服务已有
`AdminUser` 鉴权 + 管理端审计日志 + 网关 `/api/v1` 全量转发，作为代理层最省事也最安全。

接口清单：

- `GET  ""`              查询：文件内容 + 环境变量原值 + 每个爬虫的生效状态；
- `PUT  ""`              按 crawler_name 覆盖写入（只提交改动行），立即生效、重启保留；
- `POST /delete`         删除某个爬虫的文件配置，回退到环境变量。
"""

from bili_common.models import ResponseCode
from fastapi import APIRouter

from app.dependencies import AdminUser
from app.models import StandardResponse
from app.models.schemas.crawler_push import (
    CrawlerPushDeleteReq,
    CrawlerPushSaveReq,
    CrawlerPushStateResp,
)
from app.services.admin.crawler_push import CrawlerPushService, CrawlerPushValueError
from app.services.infrastructure.crawler_push import CrawlerPushError

router = APIRouter(prefix="/api/v1/crawler/admin/push-switch", tags=["crawler-push-admin"])


@router.get(
    "",
    response_model=StandardResponse[CrawlerPushStateResp],
    summary="爬虫「开始 / 结束」推送开关（文件内容 + 环境变量合并后的生效状态）",
)
async def get_push_switch(user: AdminUser) -> StandardResponse[CrawlerPushStateResp]:
    """查询开关；`effective[].source` 标明该爬虫当前生效来源（file / env）。"""
    try:
        data = await CrawlerPushService.get_state()
    except CrawlerPushError as e:
        return StandardResponse(code=int(ResponseCode.NETWORK_ERROR), msg=str(e))
    return StandardResponse(data=data)


@router.put(
    "",
    response_model=StandardResponse[CrawlerPushStateResp],
    summary="修改爬虫推送开关（写入 crawler 本地文件，立即生效、重启保留）",
)
async def save_push_switch(
    user: AdminUser, req: CrawlerPushSaveReq
) -> StandardResponse[CrawlerPushStateResp]:
    """按 crawler_name 覆盖写入；未出现在 items 里的爬虫保持原配置。

    想恢复成「跟随环境变量」，请调用 `/delete` 删掉该爬虫的文件配置。
    """
    try:
        data = await CrawlerPushService.save_items(req, operator_mid=int(user.mid))
    except CrawlerPushValueError as e:
        return StandardResponse(code=int(ResponseCode.INVALID_PARAM), msg=str(e))
    except CrawlerPushError as e:
        return StandardResponse(code=int(ResponseCode.NETWORK_ERROR), msg=str(e))
    return StandardResponse(data=data)


@router.post(
    "/delete",
    response_model=StandardResponse[CrawlerPushStateResp],
    summary="恢复某个爬虫的推送开关为环境变量",
)
async def delete_push_switch(
    user: AdminUser, req: CrawlerPushDeleteReq
) -> StandardResponse[CrawlerPushStateResp]:
    """删除该爬虫的文件配置；此后按环境变量取值（未配置时默认关闭）。"""
    try:
        data = await CrawlerPushService.delete_item(req, operator_mid=int(user.mid))
    except CrawlerPushValueError as e:
        return StandardResponse(code=int(ResponseCode.INVALID_PARAM), msg=str(e))
    except CrawlerPushError as e:
        return StandardResponse(code=int(ResponseCode.NETWORK_ERROR), msg=str(e))
    return StandardResponse(data=data)
