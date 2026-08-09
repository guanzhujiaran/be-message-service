"""消息管理端权限授权接口（自包含于 be-message-service，仅 root 可授权）。

与 RPA 权限体系解耦：授权数据存放于本服务的 `msg_admin` 表；
- `POST /grant`、`POST /revoke`、`GET /list` 仅 root 可调用；
- `GET /me` 任意登录用户可调用，用于前端判断自己是否为管理员及其权限。
"""

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from app.core.database import SessionDep
from app.dependencies import CurrentUser, MsgAdminUser, RootUser
from app.models import StandardResponse
from app.models.db.admin import MessageAdmin
from app.services.message_admin import MessageAdminService
from bili_common.models import AdminStatusResponse

router = APIRouter(prefix="/api/v1/message/admin", tags=["message-admin"])


class GrantAdminReq(BaseModel):
    mid: int = Query(..., description="被授予权限的用户 mid")
    permissions: list[str] = []
    note: str | None = None


class RevokeAdminReq(BaseModel):
    mid: int = Query(..., description="被撤销权限的用户 mid")


class AdminItem(BaseModel):
    mid: int
    granted_by: int
    permissions: list[str]
    note: str | None
    created_at: datetime | None = None


@router.post("/grant", summary="授予消息管理端权限（仅 root）")
async def grant_admin(
    session: SessionDep,
    user: RootUser,
    req: GrantAdminReq,
) -> StandardResponse[AdminItem]:
    """授予 / 更新某用户的消息管理端权限。

    仅 root 可调用；root 专属权限（查看内容明文 / 设置过审没过审）会被自动剔除，
    不会落入 `msg_admin` 表。
    """
    admin = await MessageAdminService.grant(
        session, user.mid, req.mid, req.permissions, req.note
    )
    return StandardResponse(
        data=AdminItem(
            mid=admin.mid,
            granted_by=admin.granted_by,
            permissions=admin.permissions,
            note=admin.note,
            created_at=admin.created_at,
        )
    )


@router.post("/revoke", summary="撤销消息管理端权限（仅 root）")
async def revoke_admin(
    session: SessionDep,
    user: RootUser,
    req: RevokeAdminReq,
) -> StandardResponse[dict]:
    """撤销某用户的消息管理端权限（仅 root）。"""
    ok = await MessageAdminService.revoke(session, req.mid)
    if not ok:
        return StandardResponse(code=404, msg="该用户不是消息管理端管理员")
    return StandardResponse(data={"mid": req.mid}, msg="已撤销")


@router.get("/list", summary="消息管理端管理员列表（仅 root）")
async def list_admins(
    session: SessionDep,
    user: RootUser,
    page_num: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=50),
) -> StandardResponse[dict]:
    """分页列出全部消息管理端管理员（仅 root）。"""
    items, total = await MessageAdminService.list_admins(session, page_num, page_size)
    return StandardResponse(
        data={
            "items": [
                AdminItem(
                    mid=a.mid,
                    granted_by=a.granted_by,
                    permissions=a.permissions,
                    note=a.note,
                    created_at=a.created_at,
                )
                for a in items
            ],
            "total": total,
            "page_num": page_num,
            "page_size": page_size,
        }
    )


@router.get("/me", summary="当前用户的管理端权限状态")
async def my_status(
    session: SessionDep,
    user: CurrentUser,
) -> StandardResponse[AdminStatusResponse]:
    """返回当前登录用户是否为消息管理端管理员及其权限（前端据此控制可见性）。"""
    if user.is_root:
        return StandardResponse(
            data=AdminStatusResponse(
                is_root=True, is_admin=True, permissions=["*"], mid=user.mid
            )
        )
    is_admin, perms = await MessageAdminService.get_status(session, user.mid)
    return StandardResponse(
        data=AdminStatusResponse(
            is_root=False, is_admin=is_admin, permissions=perms, mid=user.mid
        )
    )


__all__ = ["router", "AdminItem", "GrantAdminReq", "RevokeAdminReq"]
