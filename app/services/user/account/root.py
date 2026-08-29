"""超级管理员角色对象（拥有全部权限）。"""

from __future__ import annotations

from app.services.user.account.comment_admin import CommentAdminUser
from app.services.user.account.dm_admin import DmAdminUser
from app.services.user.account.user_governance import UserGovernanceUser


class RootAdminUser(CommentAdminUser, DmAdminUser, UserGovernanceUser):
    """超级管理员：拥有全部权限。

    通过多重继承组合三个治理子类，天然体现「一个账号可同时拥有多个权限」——
    实例最终持有三者 ``ROLE_PERMISSIONS`` 的并集。
    """

    ROLE_PERMISSIONS = frozenset(
        {
            *CommentAdminUser.ROLE_PERMISSIONS,
            *DmAdminUser.ROLE_PERMISSIONS,
            *UserGovernanceUser.ROLE_PERMISSIONS,
        }
    )


__all__ = ["RootAdminUser"]
