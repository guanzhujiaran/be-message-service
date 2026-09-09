"""消息管理端管理员依赖（自包含于 be-message-service）。

- root（x-bili-role=root）：恒拥有全部权限；
- 非 root：权限从本服务 `msg_admin` 表读取，并写回 `auth.biz_perms`，
  供接口内 `has_biz_perm` 做按位权限判断。
"""

from typing import Annotated

from bili_common.deps.permissions import ROOT_BIZ_PERM
from bili_common.models.depends import AuthInfo
from fastapi import Depends, HTTPException, status
from sqlmodel import select

from app.core.database import SessionDep
from app.core.viewer_context import bind_viewer
from app.dependencies.user import get_current_user
from app.models.db.admin_tbl import MessageAdmin


async def msg_admin_user(
    auth: AuthInfo = Depends(get_current_user),
    session: SessionDep = None,
) -> AuthInfo:
    """消息管理端管理员依赖：root 或本服务内被授予权限的管理员。

    非 root 管理员的权限从本服务 `msg_admin` 表读取，写回 `auth.permissions`，
    供接口内的 `has_biz_perm`（按位权限判断）使用。
    """
    if auth.is_root:
        auth.biz_perms = {"*": ROOT_BIZ_PERM}
        # 管理端需看到完整字段（如审核队列 member 的私域字段用于溯源）：
        # 提升为管理员视角，请求结束由 viewer_context_middleware 统一回滚。
        bind_viewer(mid=auth.mid, is_admin=True)
        return auth
    admin = (
        await session.exec(select(MessageAdmin).where(MessageAdmin.mid == auth.mid))
    ).first()
    if admin is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="需要管理员权限"
        )
    auth.biz_perms = admin.biz_perms or {}
    # 同上：细粒度管理员同样按管理员视角序列化（权限来自 msg_admin 表，中间件无从判定）
    bind_viewer(mid=auth.mid, is_admin=True)
    return auth


# 路由函数签名中直接使用的依赖注解类型
MsgAdminUser = Annotated[AuthInfo, Depends(msg_admin_user)]
