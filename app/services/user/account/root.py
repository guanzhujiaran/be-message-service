"""超级管理员角色对象（拥有全部权限）。"""

from __future__ import annotations

from bili_common.deps.permissions import ROOT_BIZ_PERM

from app.services.user.account.comment_admin import CommentAdminUser
from app.services.user.account.dm_admin import DmAdminUser
from app.services.user.account.user_governance import UserGovernanceUser


class RootAdminUser(CommentAdminUser, DmAdminUser, UserGovernanceUser):
    """超级管理员：拥有全部资源域的全权（rwx=7）。

    通过多重继承组合三个治理子类，天然体现「一个账号可同时拥有多个权限」——
    实例最终持有三者 ``ROLE_BIZ_PERMS`` 的按域并集，并追加全部域全权。
    """

    ROLE_BIZ_PERMS = {
        **CommentAdminUser.ROLE_BIZ_PERMS,
        **DmAdminUser.ROLE_BIZ_PERMS,
        **UserGovernanceUser.ROLE_BIZ_PERMS,
        # 全部审核域全权（与 bili_common.deps.permissions.AUDIT_BIZ_KEYS 对齐）
        **{
            biz: ROOT_BIZ_PERM
            for biz in (
                CommentAdminUser.ROLE_BIZ_PERMS.keys()
                | DmAdminUser.ROLE_BIZ_PERMS.keys()
                | UserGovernanceUser.ROLE_BIZ_PERMS.keys()
            )
        },
    }


__all__ = ["RootAdminUser"]
