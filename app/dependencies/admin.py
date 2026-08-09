"""消息管理端管理员依赖（自包含于 be-message-service）。

- root（x-bili-role=root）：恒拥有全部权限；
- 非 root：权限从本服务 `msg_admin` 表读取，并写回 `auth.permissions`，
  供接口内 `has_permission` 做内容脱敏 / 审核权限判断。
"""

from typing import Annotated

from fastapi import Depends, HTTPException, status
from sqlmodel import select

from app.core.database import SessionDep
from app.models.db.admin import MessageAdmin
from app.dependencies.user import get_current_user
from bili_common.models.depends import AuthInfo


async def msg_admin_user(
    auth: AuthInfo = Depends(get_current_user),
    session: SessionDep = None,
) -> AuthInfo:
    """消息管理端管理员依赖：root 或本服务内被授予权限的管理员。

    非 root 管理员的权限从本服务 `msg_admin` 表读取，写回 `auth.permissions`，
    供接口内的 `has_permission`（内容脱敏 / 审核权限）判断使用。
    """
    if auth.is_root:
        auth.permissions = ["*"]
        return auth
    admin = (
        await session.exec(select(MessageAdmin).where(MessageAdmin.mid == auth.mid))
    ).first()
    if admin is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="需要管理员权限"
        )
    auth.permissions = admin.permissions or []
    return auth


# 路由函数签名中直接使用的依赖注解类型
MsgAdminUser = Annotated[AuthInfo, Depends(msg_admin_user)]
