"""评论管理端角色对象（查看队列 / 内容明文 / 审核）。"""

from __future__ import annotations

from bili_common.deps.permissions import BizPermOp
from bili_common.models.interaction import InteractionBizTypeEnum
from app.services.user.account.base import PptrUser


class CommentAdminUser(PptrUser):
    """评论管理端角色：查看队列 / 内容明文 / 审核。"""

    # 评论域：查看 + 审核（rw=6）
    ROLE_BIZ_PERMS = {InteractionBizTypeEnum.COMMENT: int(BizPermOp.VIEW | BizPermOp.AUDIT)}


__all__ = ["CommentAdminUser"]
