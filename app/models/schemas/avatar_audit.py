"""头像更换审核的请求 / 响应模型。

- 用户侧：提交更换（复用 `/user_info/update`，响应扩展 avatar_status）、查询我的审核状态；
- 管理端：待审核列表 / 通过 / 驳回（RootUser 守卫）。
"""

from datetime import datetime

from sqlmodel import Field, SQLModel


class AvatarAuditItem(SQLModel):
    """管理端待审核队列中的单条头像申请（含作者昵称 / 头像，来自 pptr 回查）。"""

    pk: int = Field(description="审核记录主键")
    mid: int = Field(description="申请用户 UID")
    authorName: str | None = Field(default=None, description="申请者昵称（pptr 回查）")
    oldAvatar: str | None = Field(default=None, description="旧头像 URL")
    newAvatar: str = Field(description="申请的新头像 URL")
    auditStatus: str = Field(description="审核状态：pending/approved/rejected")
    createdAt: str | None = Field(default=None, description="提交时间（ISO）")


class AvatarAuditListResp(SQLModel):
    """管理端待审核列表响应。"""

    items: list[AvatarAuditItem] = Field(default_factory=list)
    total: int = Field(default=0, description="符合条件的总数")
    page_num: int = Field(default=1)
    page_size: int = Field(default=20)


class AvatarAuditApproveReq(SQLModel):
    """审核通过请求。"""

    pk: int = Field(description="审核记录主键")
    remark: str | None = Field(default=None, description="审核备注（选填）")


class AvatarAuditRejectReq(SQLModel):
    """审核驳回请求。"""

    pk: int = Field(description="审核记录主键")
    reason: str = Field(description="驳回原因")
    remark: str | None = Field(default=None, description="审核备注（选填）")


class AvatarAuditMineResp(SQLModel):
    """用户侧「我的头像审核状态」响应（无记录时接口返回 data=null）。"""

    pk: int = Field(description="审核记录主键")
    newAvatar: str = Field(description="申请的新头像 URL")
    oldAvatar: str | None = Field(default=None, description="旧头像 URL")
    auditStatus: str = Field(description="审核状态：pending/approved/rejected")
    auditReason: str | None = Field(default=None, description="驳回原因（rejected 时有值）")
    createdAt: str | None = Field(default=None, description="提交时间（ISO）")
    auditedAt: str | None = Field(default=None, description="审核时间（ISO）")


__all__ = [
    "AvatarAuditApproveReq",
    "AvatarAuditItem",
    "AvatarAuditListResp",
    "AvatarAuditMineResp",
    "AvatarAuditRejectReq",
]
