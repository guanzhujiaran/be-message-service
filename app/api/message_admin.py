"""消息管理端权限授权接口（自包含于 be-message-service，仅 root 可授权）。

与 RPA 权限体系解耦：授权数据存放于本服务的 `msg_admin` 表；
- `POST /grant`、`POST /revoke`、`GET /list` 仅 root 可调用；
- `GET /me` 任意登录用户可调用，用于前端判断自己是否为管理员及其权限。
"""

from datetime import datetime

from bili_common.models import AdminStatusResponse
from fastapi import APIRouter, Query
from pydantic import BaseModel

from app.core.database import SessionDep
from app.dependencies import CurrentUser, RootUser
from app.models import StandardResponse
from app.models.str_int import StrInt
from app.services.admin.message_admin import MessageAdminService

router = APIRouter(prefix="/api/v1/message/admin", tags=["message-admin"])


class GrantAdminReq(BaseModel):
    mid: StrInt = Query(..., description="被授予权限的用户 mid（雪花 ID，StrInt 兼容前端 str 传参）")
    # 管理端权限（per-biz 位掩码）：键=资源域文本（dynamic/dm/…），值=权限字 0~7
    biz_perms: dict[str, int] = {}
    note: str | None = None


class RevokeAdminReq(BaseModel):
    mid: StrInt = Query(..., description="被撤销权限的用户 mid（雪花 ID，StrInt 兼容前端 str 传参）")


class AdminItem(BaseModel):
    mid: int
    granted_by: int
    biz_perms: dict[str, int]
    note: str | None
    created_at: datetime | None = None


@router.post("/grant", summary="授予消息管理端权限（仅 root）")
async def grant_admin(
    session: SessionDep,
    user: RootUser,
    req: GrantAdminReq,
) -> StandardResponse[AdminItem]:
    """授予 / 更新某用户的消息管理端权限（per-biz 位掩码权限字，仅 root 调用）。

    biz_perms 键为资源域文本（dynamic/comment/…），值为权限字 0~7（rwx 位掩码）；
    落库前经 normalize_biz_perms 清洗（非法键丢弃、值截断到 3 位）。
    """
    admin = await MessageAdminService.grant(
        session, user.mid, req.mid, req.biz_perms, req.note
    )
    return StandardResponse(
        data=AdminItem(
            mid=admin.mid,
            granted_by=admin.granted_by,
            biz_perms=admin.biz_perms,
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
                    biz_perms=a.biz_perms,
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
                is_root=True, is_admin=True, biz_perms={"*": 7}, mid=user.mid
            )
        )
    is_admin, perms = await MessageAdminService.get_status(session, user.mid)
    return StandardResponse(
        data=AdminStatusResponse(
            is_root=False, is_admin=is_admin, biz_perms=perms, mid=user.mid
        )
    )


__all__ = ["AdminItem", "GrantAdminReq", "RevokeAdminReq", "router"]
