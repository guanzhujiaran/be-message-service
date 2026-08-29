"""pptr 账号领域对象包。

把「一个 pptr 账号」建模为可继承的用户对象：构造只传一个 uid，
档案相关方法按需懒加载；按权限派生出评论 / 私信管理端、用户治理、超级管理员子类。
"""

from __future__ import annotations

from app.services.user.account.base import PptrUser
from app.services.user.account.comment_admin import CommentAdminUser
from app.services.user.account.dm_admin import DmAdminUser
from app.services.user.account.user_governance import UserGovernanceUser
from app.services.user.account.root import RootAdminUser

__all__ = [
    "PptrUser",
    "CommentAdminUser",
    "DmAdminUser",
    "UserGovernanceUser",
    "RootAdminUser",
]
