"""收藏夹封面审核接口（对齐头像审核 `avatar_audit.py`）。

- ``GET  /mine``        当前登录用户某收藏夹封面审核状态（CurrentUser，仅本人）
- ``GET  /list``        管理员待审核封面队列（RootUser）
- ``POST /approve``     审核通过：newCover 写入收藏夹封面 + 通知用户（RootUser）
- ``POST /reject``      审核驳回：保持原封面 + 通知用户（RootUser）

管理端接口仅 ``role=root`` 可访问（RootUser 守卫，与动态审核一致）。
"""

from fastapi import APIRouter, Query

from app.core.database import SessionDep
from app.dependencies import CurrentUser, RootUser
from app.models import StandardResponse
from app.models.schemas.folder_cover_audit import (
    FolderCoverAuditApproveReq,
    FolderCoverAuditListResp,
    FolderCoverAuditMineResp,
    FolderCoverAuditRejectReq,
)
from app.services.user.folder_cover_audit import FolderCoverAuditService

router = APIRouter(prefix="/api/v1/favorite/folder/cover/audit", tags=["folder-cover-audit"])


def _parse_folder_id(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


@router.get(
    "/mine",
    response_model=StandardResponse[FolderCoverAuditMineResp | None],
    summary="我的某收藏夹封面审核状态",
)
async def folder_cover_audit_mine(
    session: SessionDep,
    user: CurrentUser,
    folderId: str = Query(description="收藏夹id（字符串）"),
) -> StandardResponse[FolderCoverAuditMineResp | None]:
    folder_id = _parse_folder_id(folderId)
    if folder_id is None:
        return StandardResponse(code=400, msg="folderId 不合法")
    data = await FolderCoverAuditService.mine(
        session, uid=int(user.mid), folder_id=folder_id
    )
    return StandardResponse(data=data)


@router.get(
    "/list",
    response_model=StandardResponse[FolderCoverAuditListResp],
    summary="管理员待审核封面列表",
)
async def folder_cover_audit_list(
    session: SessionDep,
    user: RootUser,
    page_num: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=50),
) -> StandardResponse[FolderCoverAuditListResp]:
    data = await FolderCoverAuditService.pending_list(
        session, page_num=page_num, page_size=page_size
    )
    return StandardResponse(data=data)


@router.post(
    "/approve",
    response_model=StandardResponse[FolderCoverAuditListResp],
    summary="审核通过收藏夹封面",
)
async def folder_cover_audit_approve(
    session: SessionDep,
    user: RootUser,
    req: FolderCoverAuditApproveReq,
) -> StandardResponse[FolderCoverAuditListResp]:
    try:
        await FolderCoverAuditService.approve(
            session, req.pk, operator_mid=user.mid, remark=req.remark
        )
    except ValueError as e:
        return StandardResponse(code=404, msg=str(e))
    return StandardResponse(msg="审核通过")


@router.post(
    "/reject",
    response_model=StandardResponse[FolderCoverAuditListResp],
    summary="审核驳回收藏夹封面",
)
async def folder_cover_audit_reject(
    session: SessionDep,
    user: RootUser,
    req: FolderCoverAuditRejectReq,
) -> StandardResponse[FolderCoverAuditListResp]:
    try:
        await FolderCoverAuditService.reject(
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
