"""管理端：be-crawler 云端 LLM 配置（/api/v1/crawler/admin/llm，2.7x）。

为什么放在 be-message：crawler 的 `/llm/*` 无鉴权且不在网关转发白名单内，
本服务已有 `AdminUser` 鉴权 + 网关 `/api/v1` 全量转发，作为代理层最省事也最安全。

接口清单：

- `GET  /config`  读取当前实例配置 + 运行统计（页面一次请求渲染表格）；
- `PUT  /config`  整体覆盖保存（热更新，无需重启 crawler）。

只提供这两个接口：统计与配置总是一起读（页面需要按 `base_url + model_name`
把统计合并到配置行上），单独开 `/stats` 只会多一次无意义的配置读取。
"""

from bili_common.models import ResponseCode
from fastapi import APIRouter

from app.dependencies import AdminUser
from app.models import StandardResponse
from app.models.schemas.crawler_llm import CrawlerLlmConfigResp, CrawlerLlmSaveReq
from app.services.admin.crawler_llm import CrawlerLlmService, CrawlerLlmValueError
from app.services.infrastructure.crawler_llm import CrawlerLlmError

router = APIRouter(prefix="/api/v1/crawler/admin/llm", tags=["crawler-llm-admin"])


@router.get(
    "/config",
    response_model=StandardResponse[CrawlerLlmConfigResp],
    summary="crawler LLM 实例配置 + 运行统计",
)
async def get_llm_config(user: AdminUser) -> StandardResponse[CrawlerLlmConfigResp]:
    """读取 crawler 当前生效的 LLM 实例（token 脱敏）及其运行统计。"""
    try:
        data = await CrawlerLlmService.get_config()
    except CrawlerLlmError as e:
        return StandardResponse(code=int(ResponseCode.NETWORK_ERROR), msg=str(e))
    return StandardResponse(data=data)


@router.put(
    "/config",
    response_model=StandardResponse[CrawlerLlmConfigResp],
    summary="整体覆盖保存 crawler LLM 实例配置",
)
async def save_llm_config(
    user: AdminUser, req: CrawlerLlmSaveReq
) -> StandardResponse[CrawlerLlmConfigResp]:
    """整体覆盖保存（顺序即索引）。

    - 数值字段一律字符串传输（`requests_per_second` 如 `"0.5"`）；
    - `token` 传空串或脱敏值表示**沿用原密钥**，新增实例必须填明文；
    - 只在 crawler 进程内存生效，**重启后回退 `.env` 的 `llm_apis`**。
    """
    try:
        data = await CrawlerLlmService.save_config(req, operator_mid=int(user.mid))
    except CrawlerLlmValueError as e:
        return StandardResponse(code=int(ResponseCode.INVALID_PARAM), msg=str(e))
    except CrawlerLlmError as e:
        return StandardResponse(code=int(ResponseCode.NETWORK_ERROR), msg=str(e))
    return StandardResponse(data=data)
