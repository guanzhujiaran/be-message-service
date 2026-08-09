"""用户封禁管理接口（审核联动，/api/v1/message/admin）。

与评论 / 私信审核配套：管理员在审核队列里对违规用户执行封禁 / 解封，
禁止其继续使用对应服务（评论 / 私信）。封禁数据自包含于本服务
（`msg_user_ban`），不回写 pptr。

权限分层（与 `bili_common.deps.permissions.UserPermission` 对齐）：
- `comment:ban`   —— 封禁 / 解封用户在评论服务（写入操作）；
- `dm:ban`        —— 封禁 / 解封用户在私信服务（写入操作）；
- `user:ban-view` —— 查看封禁记录与状态（只读，跨服务）。

封禁 / 解封按「服务维度」分别鉴权：请求里 `ban_services` 含 `comment` 则需
`comment:ban`、含 `dm` 则需 `dm:ban`，缺任一服务的权限即整体拒绝。
上述权限均 grantable，可由 root 授予非 root 管理员。
"""

from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlmodel import SQLModel

from app.core.database import SessionDep
from app.dependencies import CurrentUser, require_permission, UserPermission
from app.models import StandardResponse
from app.models.db.ban import UserBan
from app.models.enums import BanDurationTypeEnum, BanStatusEnum
from app.models.schemas import (
    BanCreateReq,
    BanListResp,
    BanStatusResp,
    UnbanReq,
)
from app.services.ban_service import BanService
from bili_common.models.depends import AuthInfo

router = APIRouter(prefix="/api/v1/message/admin", tags=["message-admin-ban"])


class _BanRes(SQLModel):
    banned_count: int = 0
    reason: str = ""


class _UnbanRes(SQLModel):
    lifted_count: int = 0


# 服务维度 → 所需权限 的映射，用于封禁 / 解封时按服务逐项鉴权
_SERVICE_BAN_PERM: dict[str, UserPermission] = {
    "comment": UserPermission.COMMENT_BAN,
    "dm": UserPermission.DM_BAN,
}


def _check_ban_permissions(user: AuthInfo, services: list[str] | None) -> str | None:
    """校验用户对每个目标服务都拥有对应封禁权限。

    返回缺失权限的服务名（首个），全部满足则返回 None。
    `services` 为 None / 空时由调用方另行处理（解封全服务场景需拥有全部权限）。
    """
    for svc in services or []:
        perm = _SERVICE_BAN_PERM.get(svc)
        if perm is None:
            continue
        if not user.has_permission(perm.value):
            return svc
    return None


@router.post("/ban", operation_id="ban_users", summary="封禁用户（按服务）")
async def ban_users(
    session: SessionDep,
    user: CurrentUser,
    req: BanCreateReq,
) -> StandardResponse[_BanRes]:
    """批量封禁用户（审核联动）。

    按服务维度（comment / dm）封禁，给出理由与封禁时长
    （temporary + duration_days 限时 / permanent 永久）。
    按服务逐项鉴权：请求含 comment 需 comment:ban、含 dm 需 dm:ban。
    """
    if not req.mids:
        return StandardResponse(code=400, msg="mids 不能为空")
    if not req.ban_services:
        return StandardResponse(code=400, msg="ban_services 不能为空")
    if req.duration_type == BanDurationTypeEnum.TEMPORARY and not req.duration_days:
        return StandardResponse(code=400, msg="限时封禁必须指定 duration_days")

    missing = _check_ban_permissions(user, req.ban_services)
    if missing is not None:
        needed = _SERVICE_BAN_PERM[missing].value
        return StandardResponse(code=403, msg=f"缺少封禁权限：{needed}（服务 {missing}）")

    count = await BanService.ban_users(
        session,
        operator_mid=user.mid,
        mids=req.mids,
        ban_services=req.ban_services,
        reason=req.reason,
        duration_type=req.duration_type,
        duration_days=req.duration_days,
    )
    return StandardResponse(data=_BanRes(banned_count=count, reason=req.reason))


@router.post("/unban", operation_id="unban_users", summary="解封用户（按服务）")
async def unban_users(
    session: SessionDep,
    user: CurrentUser,
    req: UnbanReq,
) -> StandardResponse[_UnbanRes]:
    """批量解封用户（审核联动）。

    不传 `ban_services` 时解封该用户全部服务（需拥有 comment:ban 与 dm:ban 全部权限）；
    传则仅解除指定服务，并逐项校验对应权限。
    """
    if not req.mids:
        return StandardResponse(code=400, msg="mids 不能为空")

    # 未指定服务 = 解封全部，要求拥有全部服务封禁权限
    target_services = req.ban_services or list(_SERVICE_BAN_PERM.keys())
    missing = _check_ban_permissions(user, target_services)
    if missing is not None:
        needed = _SERVICE_BAN_PERM[missing].value
        return StandardResponse(code=403, msg=f"缺少解封权限：{needed}（服务 {missing}）")

    count = await BanService.unban_users(session, req.mids, req.ban_services)
    return StandardResponse(data=_UnbanRes(lifted_count=count))


@router.get(
    "/ban/list",
    operation_id="list_bans",
    summary="封禁记录列表",
)
async def list_bans(
    session: SessionDep,
    user: Annotated[AuthInfo, Depends(require_permission(UserPermission.USER_BAN_VIEW))],
    status: BanStatusEnum | None = Query(default=None, description="过滤状态：active / lifted"),
    page_num: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=50),
) -> StandardResponse[BanListResp]:
    """分页查看封禁记录（root 或拥有 `user:ban-view` 权限的管理员）。"""
    items, total = await BanService.list_bans(
        session, status=status, page_num=page_num, page_size=page_size
    )
    return StandardResponse(
        data=BanListResp(
            items=items, total=total, page_num=page_num, page_size=page_size
        )
    )


@router.get(
    "/ban/status",
    operation_id="ban_status",
    summary="查询用户封禁状态",
)
async def ban_status(
    session: SessionDep,
    user: Annotated[AuthInfo, Depends(require_permission(UserPermission.USER_BAN_VIEW))],
    mid: int = Query(..., description="待查询用户 mid"),
) -> StandardResponse[BanStatusResp]:
    """查询某用户在各服务的封禁状态（实时计算到期）。"""
    data = await BanService.get_status(session, mid)
    return StandardResponse(data=data)


__all__ = ["router"]
