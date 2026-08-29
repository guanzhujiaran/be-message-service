"""私信管理端角色对象（查看队列 / 内容明文 / 审核）。"""

from __future__ import annotations

from bili_common.deps.permissions import UserPermission
from app.services.user.account.base import PptrUser


class DmAdminUser(PptrUser):
    """私信管理端角色：查看队列 / 内容明文 / 审核。"""

    ROLE_PERMISSIONS = frozenset(
        {
            UserPermission.DM_VIEW_QUEUE,
            UserPermission.DM_VIEW_CONTENT,
            UserPermission.DM_AUDIT,
        }
    )


__all__ = ["DmAdminUser"]
