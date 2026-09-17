"""收藏夹封面审核的请求 / 响应模型。

- 用户侧：创建/更新收藏夹携带 `coverUrl` 提交封面审核（响应新增 `coverAuditStatus`）、
  查询某夹封面审核状态（`mine`）；
- 管理端：待审核列表 / 通过 / 驳回（RootUser 守卫）。
"""

from datetime import datetime

from sqlmodel import Field, SQLModel


from app.models.schemas.base import auto_str
@auto_str
class FolderCoverAuditItem(SQLModel):
    """管理端待审核队列中的单条封面申请（含作者昵称，来自 pptr 回查）。"""

    pk: int = Field(description="审核记录主键")
    folderId: str = Field(description="所属收藏夹 id（字符串）")
    mid: int = Field(description="提交封面的用户 UID")
    authorName: str | None = Field(default=None, description="提交者昵称（pptr 回查）")
    oldCover: str | None = Field(default=None, description="旧封面 URL")
    newCover: str = Field(description="申请的新封面 URL")
    auditStatus: str = Field(description="审核状态（统一枚举 .name）：AUDITING=待审 / NORMAL=已通过 / REJECTED=已驳回")
    createdAt: str | None = Field(default=None, description="提交时间（ISO）")


@auto_str
class FolderCoverAuditListResp(SQLModel):
    """管理端待审核列表响应。"""

    items: list[FolderCoverAuditItem] = Field(default_factory=list)
    total: int = Field(default=0, description="符合条件的总数")
    page_num: int = Field(default=1)
    page_size: int = Field(default=20)


@auto_str
class FolderCoverAuditApproveReq(SQLModel):
    """审核通过请求。"""

    pk: int = Field(description="审核记录主键")
    remark: str | None = Field(default=None, description="审核备注（选填）")


@auto_str
class FolderCoverAuditRejectReq(SQLModel):
    """审核驳回请求。"""

    pk: int = Field(description="审核记录主键")
    reason: str = Field(description="驳回原因")
    remark: str | None = Field(default=None, description="审核备注（选填）")


@auto_str
class FolderCoverAuditMineResp(SQLModel):
    """用户侧「某收藏夹封面审核状态」响应（无记录时接口返回 data=null）。"""

    pk: int = Field(description="审核记录主键")
    folderId: str = Field(description="所属收藏夹 id（字符串）")
    newCover: str = Field(description="申请的新封面 URL")
    oldCover: str | None = Field(default=None, description="旧封面 URL")
    auditStatus: str = Field(description="审核状态（统一枚举 .name）：AUDITING=待审 / NORMAL=已通过 / REJECTED=已驳回")
    auditReason: str | None = Field(default=None, description="驳回原因（REJECTED 时有值）")
    createdAt: str | None = Field(default=None, description="提交时间（ISO）")
    auditedAt: str | None = Field(default=None, description="审核时间（ISO）")


__all__ = [
    "FolderCoverAuditApproveReq",
    "FolderCoverAuditItem",
    "FolderCoverAuditListResp",
    "FolderCoverAuditMineResp",
    "FolderCoverAuditRejectReq",
]
