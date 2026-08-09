"""FastAPI 依赖（Depends）集中存放目录，供各 api 路由复用。"""

from app.dependencies.user import (
    AdminUser,
    CurrentUser,
    RequiredUser,
    RootUser,
    get_admin_user,
    get_current_user,
    require_permission,
    require_root,
)
from app.dependencies.admin import MsgAdminUser
from bili_common.deps.permissions import ROOT_ONLY_PERMISSIONS, UserPermission

__all__ = [
    "CurrentUser",
    "RequiredUser",
    "AdminUser",
    "RootUser",
    "MsgAdminUser",
    "get_admin_user",
    "get_current_user",
    "require_root",
    "require_permission",
    "UserPermission",
    "ROOT_ONLY_PERMISSIONS",
]
