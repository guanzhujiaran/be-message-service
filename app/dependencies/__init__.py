"""FastAPI 依赖（Depends）集中存放目录，供各 api 路由复用。"""

from bili_common.deps.permissions import ROOT_ONLY_PERMISSIONS, UserPermission

from app.dependencies.admin import MsgAdminUser
from app.dependencies.user import (
    AdminUser,
    CurrentUser,
    OptionalUser,
    RequiredUser,
    RootUser,
    get_admin_user,
    get_current_user,
    get_optional_user,
    require_permission,
    require_root,
)

__all__ = [
    "ROOT_ONLY_PERMISSIONS",
    "AdminUser",
    "CurrentUser",
    "MsgAdminUser",
    "OptionalUser",
    "RequiredUser",
    "RootUser",
    "UserPermission",
    "get_admin_user",
    "get_current_user",
    "get_optional_user",
    "require_permission",
    "require_root",
]
