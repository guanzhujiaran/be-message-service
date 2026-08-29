"""评论管理端角色对象（查看队列 / 内容明文 / 审核）。"""

from __future__ import annotations

from bili_common.deps.permissions import UserPermission
from app.services.user.account.base import PptrUser


class CommentAdminUser(PptrUser):
    """评论管理端角色：查看队列 / 内容明文 / 审核。"""

    ROLE_PERMISSIONS = frozenset(
        {
            UserPermission.COMMENT_VIEW_QUEUE,
            UserPermission.COMMENT_VIEW_CONTENT,
            UserPermission.COMMENT_AUDIT,
        }
    )


__all__ = ["CommentAdminUser"]
