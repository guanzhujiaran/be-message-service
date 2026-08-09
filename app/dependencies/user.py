"""用户相关的 FastAPI 依赖（Depends）。

用户信息优先来自上游 be-gateway 注入的 x-bili-* 请求头（网关 /identify
成功时注入）。当 x-bili-mid 为空时（网关 /identify 降级，如 be-message
短暂不可用导致中间件 catch 回退），回退到 x-bili-jwt 头中的原始 JWT，
由本服务 jwt_service.decode_token 验签并提取 uid/user_name/level/role，
避免持有合法 JWT 的用户被误判为未登录。

解析逻辑统一复用 bili_common 的 `get_auth_info_from_header`：
- 自动从 x-bili-* 头还原为 AuthInfo（统一认证模型）；
- 缺 x-bili-mid 且 JWT 回退也失败时抛 401（NotLoggedInException）；
- 管理员依赖 AdminUser 也由本模块统一提供（role=root 校验）。

消息管理端（评论 / 私信审核）的细粒度授权自包含于本服务：非 root 管理员的
权限从 `msg_admin` 表读取，由 `MsgAdminUser` 依赖把关。
"""

from typing import Annotated

from fastapi import Depends, Header, HTTPException, status

from bili_common.deps.auth import (
    get_auth_info_from_header,
    require_permission as _require_permission,
    require_root as _require_root,
)
from bili_common.deps.permissions import UserPermission, ROOT_ONLY_PERMISSIONS
from bili_common.models.depends import AuthInfo
from bili_common.exceptions import NotLoggedInException
from app.services import jwt_service


async def get_current_user(
    x_bili_mid: str | None = Header(default=None),
    x_bili_jwt: str | None = Header(default=None),
    x_bili_level: str | None = Header(default=None),
    x_bili_role: str = Header(default="normal"),
    x_bili_permissions: str | None = Header(default=None),
    x_bili_user_name: str | None = Header(default=None),
    x_bili_uname: str | None = Header(default=None),
    x_bili_sign: str | None = Header(default=None),
    x_bili_sex: str | None = Header(default=None),
    x_bili_email: str | None = Header(default=None),
    x_bili_vip_status: str | None = Header(default=None),
    x_bili_vip_type: str | None = Header(default=None),
) -> AuthInfo:
    """带 JWT 回退的用户身份解析。

    优先使用 x-bili-mid（网关 /identify 成功时注入）；
    x-bili-mid 为空时回退到 x-bili-jwt（网关 /identify 降级时透传），
    从 JWT 载荷提取 uid/user_name/level/role 构造 AuthInfo。
    """
    if not x_bili_mid and x_bili_jwt:
        payload = jwt_service.decode_token(x_bili_jwt)
        if payload and payload.get("uid") is not None:
            x_bili_mid = str(payload["uid"])
            if not x_bili_level:
                x_bili_level = str(payload.get("level", ""))
            if x_bili_role == "normal":
                x_bili_role = str(payload.get("role", "normal"))
            if not x_bili_user_name:
                x_bili_user_name = payload.get("user_name", "")

    return get_auth_info_from_header(
        x_bili_mid=x_bili_mid,
        x_bili_level=x_bili_level,
        x_bili_role=x_bili_role,
        x_bili_permissions=x_bili_permissions,
        x_bili_user_name=x_bili_user_name,
        x_bili_uname=x_bili_uname,
        x_bili_sign=x_bili_sign,
        x_bili_sex=x_bili_sex,
        x_bili_email=x_bili_email,
        x_bili_vip_status=x_bili_vip_status,
        x_bili_vip_type=x_bili_vip_type,
    )


async def get_admin_user(
    auth: Annotated[AuthInfo, Depends(get_current_user)],
) -> AuthInfo:
    """管理员依赖：仅 role=root 可访问。"""
    if auth.role != "root":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="仅管理员可执行该操作"
        )
    return auth


# CurrentUser 与 RequiredUser 等价：get_current_user 在缺 x-bili-mid 且
# JWT 回退也失败时已直接抛 401，并不存在「可选 / 匿名」分支，故两者指向同一依赖。
CurrentUser = Annotated[AuthInfo, Depends(get_current_user)]
RequiredUser = CurrentUser

# root 专属：查看全部评论/私信内容明文、设置过审/没过审。等价于 AdminUser（role=root）。
AdminUser = Annotated[AuthInfo, Depends(get_admin_user)]
RootUser = AdminUser

require_root = _require_root
require_permission = _require_permission
