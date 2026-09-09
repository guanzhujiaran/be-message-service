"""头像更换审核接口。

- ``GET  /mine``         当前登录用户「我的头像审核状态」（CurrentUser，仅本人）
- ``GET  /list``         管理员待审核列表（RootUser）
- ``POST /approve``      审核通过：newAvatar 写入公开头像 + 通知用户（RootUser）
- ``POST /reject``       审核驳回：保持旧头像 + 通知用户（RootUser）

管理端接口仅 ``role=root`` 可访问（RootUser 守卫，与动态审核一致）。
"""

from fastapi import APIRouter, Query

from app.core.database import SessionDep
from app.dependencies import CurrentUser, RootUser
from app.models import StandardResponse
from app.models.enums import ResourceAuditStatusEnum
from app.models.schemas.avatar_audit import (
    AvatarAuditApproveReq,
    AvatarAuditListResp,
    AvatarAuditMineResp,
    AvatarAuditRejectReq,
)
from app.services.user.avatar_audit import AvatarAuditService

router = APIRouter(prefix="/api/v1/user/avatar/audit", tags=["avatar-audit"])


@router.get(
    "/mine",
    response_model=StandardResponse[AvatarAuditMineResp | None],
    summary="我的头像审核状态",
)
async def avatar_audit_mine(
    session: SessionDep,
    user: CurrentUser,
) -> StandardResponse[AvatarAuditMineResp | None]:
    data = await AvatarAuditService.mine(session, uid=int(user.mid))
    return StandardResponse(data=data)


@router.get(
    "/list",
    response_model=StandardResponse[AvatarAuditListResp],
    summary="管理员待审核头像列表",
)
async def avatar_audit_list(
    session: SessionDep,
    user: RootUser,
    auditStatus: ResourceAuditStatusEnum = Query(
        default=ResourceAuditStatusEnum.AUDITING,
        description="审核状态筛选：auditing（默认）/normal/rejected/hidden",
    ),
    page_num: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=50),
) -> StandardResponse[AvatarAuditListResp]:
    data = await AvatarAuditService.pending_list(
        session, audit_status=auditStatus, page_num=page_num, page_size=page_size
    )
    return StandardResponse(data=data)


@router.post(
    "/approve",
    response_model=StandardResponse[AvatarAuditListResp],
    summary="审核通过头像更换",
)
async def avatar_audit_approve(
    session: SessionDep,
    user: RootUser,
    req: AvatarAuditApproveReq,
) -> StandardResponse[AvatarAuditListResp]:
    try:
        await AvatarAuditService.approve(
            session, req.pk, operator_mid=user.mid, remark=req.remark
        )
    except ValueError as e:
        return StandardResponse(code=404, msg=str(e))
    return StandardResponse(msg="审核通过")


@router.post(
    "/reject",
    response_model=StandardResponse[AvatarAuditListResp],
    summary="审核驳回头像更换",
)
async def avatar_audit_reject(
    session: SessionDep,
    user: RootUser,
    req: AvatarAuditRejectReq,
) -> StandardResponse[AvatarAuditListResp]:
    try:
        await AvatarAuditService.reject(
            session,
            req.pk,
            operator_mid=user.mid,
            reason=req.reason,
            remark=req.remark,
        )
    except ValueError as e:
        return StandardResponse(code=404, msg=str(e))
    return StandardResponse(msg="审核驳回")


__all__ = ["router"]
