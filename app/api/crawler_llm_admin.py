"""管理端：be-crawler 云端 LLM 配置（/api/v1/crawler/admin/llm，2.7x）。

为什么放在 be-message：crawler 的 `/llm/*` 无鉴权且不在网关转发白名单内，
本服务已有 `AdminUser` 鉴权 + 网关 `/api/v1` 全量转发，作为代理层最省事也最安全。

接口清单：

- `GET    /config`       读取当前实例配置 + 运行统计（页面一次请求渲染表格）；
- `PUT    /config`       整体覆盖保存（热更新 + 落盘 runtime/llm_apis.json）；
- `DELETE /config`       撤销人工保存：删持久化文件并热切回 `.env` 的 `llm_apis`
  （crawler 内部 `DELETE /llm/config/override` 的代理，幂等）；
- `POST   /config/test`  单条实例连通性探测（真实 LLM 调用；crawler 内部 `POST /llm/config/test` 的代理）。

统计与配置总是一起读（页面需要按 `base_url + model_name` 把统计合并到配置卡上），
单独开 `/stats` 只会多一次无意义的配置读取。
"""

from bili_common.models import ResponseCode
from fastapi import APIRouter

from app.dependencies import AdminUser
from app.models import StandardResponse
from app.models.schemas.crawler_llm import (
    CrawlerLlmConfigResp,
    CrawlerLlmSaveReq,
    CrawlerLlmTestReq,
    CrawlerLlmTestResp,
)
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
    - 保存后立即热更新，并**持久化到 crawler 的 `runtime/llm_apis.json`**，
      crawler 重启后仍然生效（事实来源：本地文件 > `.env`，不回写 `.env`）。
    """
    try:
        data = await CrawlerLlmService.save_config(req, operator_mid=int(user.mid))
    except CrawlerLlmValueError as e:
        return StandardResponse(code=int(ResponseCode.INVALID_PARAM), msg=str(e))
    except CrawlerLlmError as e:
        return StandardResponse(code=int(ResponseCode.NETWORK_ERROR), msg=str(e))
    return StandardResponse(data=data)


@router.post(
    "/config/test",
    response_model=StandardResponse[CrawlerLlmTestResp],
    summary="测试单条 crawler LLM 实例是否可用",
)
async def test_llm_config(
    user: AdminUser, req: CrawlerLlmTestReq
) -> StandardResponse[CrawlerLlmTestResp]:
    """对一条实例做一次真实 LLM 调用探测（可测未保存的编辑态）。

    - `token` 传空串或脱敏值 → 按 `index` 沿用原密钥（新增行必须传明文）；
    - 只做诊断：不参与 crawler 的健康状态机、不改统计、不落盘；
    - 上游慢模型可能触发探测超时（crawler `llm_test_timeout`，默认 45s），
      此时返回 `ok=false` + 超时文案，不代表配置错误。
    """
    try:
        data = await CrawlerLlmService.test_config(req, operator_mid=int(user.mid))
    except CrawlerLlmValueError as e:
        return StandardResponse(code=int(ResponseCode.INVALID_PARAM), msg=str(e))
    except CrawlerLlmError as e:
        return StandardResponse(code=int(ResponseCode.NETWORK_ERROR), msg=str(e))
    return StandardResponse(data=data)


@router.delete(
    "/config",
    response_model=StandardResponse[CrawlerLlmConfigResp],
    summary="撤销人工保存，恢复为 .env 配置",
)
async def reset_llm_config(user: AdminUser) -> StandardResponse[CrawlerLlmConfigResp]:
    """撤销人工保存：删除 crawler 的持久化文件并热切回 `.env` 的 `llm_apis`。

    - 幂等：本来就没有持久化文件时也返回成功（相当于把 `.env` 重新应用一遍）；
    - 返回切换后的配置 + 统计（与 `GET` 同结构，页面可直接覆盖表格）；
    - 会丢弃运维在管理页保存过的所有人工修改，前端必须二次确认。
    """
    try:
        data = await CrawlerLlmService.reset_config(operator_mid=int(user.mid))
    except CrawlerLlmError as e:
        return StandardResponse(code=int(ResponseCode.NETWORK_ERROR), msg=str(e))
    return StandardResponse(data=data)
