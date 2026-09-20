"""运行时系统配置 HTTP 接口（/api/v1/message/admin/sys-config，2.64.0）。

管理端专用（`AdminUser`，role=root）：列出 / 写入 `msg_sys_config`。

- `GET  /admin/sys-config`         列出已写入的配置项（未写入的项按 settings 默认值生效，
  不会出现在列表里，属预期）。
- `POST /admin/sys-config/update`  写入（整体替换）某个配置项——**这是「不重启改参数」的
  入口**：本实例写完立即生效，其余实例 ≤ `sys_config_cache_ttl_seconds`（默认 10s）。

配置项清单与值结构由 `app/services/common/runtime_config.py` 的 `CONFIG_SPECS` 登记
（值模型 SQLModel + 默认值提供者），接口层不做类型白名单（新增配置项只需登记一次）。
"""

from fastapi import APIRouter

from app.core.database import SessionDep
from app.dependencies import AdminUser
from app.models import StandardResponse
from app.models.schemas import SysConfigItem, SysConfigListResp, SysConfigUpdateReq
from app.services.admin.sys_config import SysConfigService, SysConfigValueError

router = APIRouter(prefix="/api/v1/message/admin/sys-config", tags=["sys-config-admin"])


@router.get(
    "",
    response_model=StandardResponse[SysConfigListResp],
    summary="运行时配置列表",
)
async def list_sys_config(
    session: SessionDep, user: AdminUser
) -> StandardResponse[SysConfigListResp]:
    """列出已写入的运行时配置项（含值、备注、最后修改人）。"""
    items = await SysConfigService.list_all(session)
    return StandardResponse(data=SysConfigListResp(items=items))


@router.post(
    "/update",
    response_model=StandardResponse[SysConfigItem],
    summary="写入运行时配置（热更新，无需重启）",
)
async def update_sys_config(
    session: SessionDep, user: AdminUser, req: SysConfigUpdateReq
) -> StandardResponse[SysConfigItem]:
    """写入（整体替换）某个运行时配置项。

    key 未登记或值结构非法一律回 400 且不落库；成功后本实例立即生效，
    其余实例最迟在缓存 TTL 内生效。
    """
    try:
        item = await SysConfigService.update(session, req, operator_mid=user.mid)
    except SysConfigValueError as e:
        return StandardResponse(code=400, msg=str(e))
    return StandardResponse(data=item)
