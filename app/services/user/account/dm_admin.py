"""私信管理端角色对象（查看队列 / 内容明文 / 审核）。"""

from __future__ import annotations

from bili_common.deps.permissions import BizPermOp
from bili_common.models.interaction import InteractionBizTypeEnum
from app.services.user.account.base import PptrUser


class DmAdminUser(PptrUser):
    """私信管理端角色：查看队列 / 内容明文 / 审核。"""

    # 私信域：查看 + 审核（rwx 中的 rw=6；内容明文 root 专属，不占权限位）
    ROLE_BIZ_PERMS = {InteractionBizTypeEnum.DM: int(BizPermOp.VIEW | BizPermOp.AUDIT)}


__all__ = ["DmAdminUser"]
