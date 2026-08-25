"""收藏夹封面审核表 ORM 模型（be-message MySQL 主库 `BiliMessageDB`）。

收藏夹封面「先审后发」（对齐头像审核 `TUserAvatarAudit` 模式）：
- 用户提交收藏夹封面（创建/更新收藏夹携带 `coverUrl`）→ 本表插入一条
  `auditStatus=pending` 记录，`TFavoriteFolder.cover_url` **保持原封面**（新夹为空）；
- 审核通过 → `auditStatus=approved`，`newCover` 被写入 `TFavoriteFolder.cover_url`
  对外公开（**同库同事务**）；
- 审核驳回 → `auditStatus=rejected`，保持原封面，`auditReason` 记录驳回原因。

约束（对齐 `app.models.db` 既有规范）：
- 表名沿用 `"T"` 前缀 PascalCase（TFolderCoverAudit）；
- 列名使用 camelCase，Python 属性名与数据库列名完全一致；
- 时间戳用 `TimestampMixin`；
- `mid` / `folderId` 系字段仅存 BIGINT，不建跨表/跨库外键。
"""

from datetime import datetime

from sqlalchemy import BIGINT, Index, PrimaryKeyConstraint, text
from sqlmodel import Field

from app.models.db.base import TimestampMixin
from sqlalchemy import Enum as SAEnum
from app.models.enums import FolderCoverAuditStatusEnum


class TFolderCoverAudit(TimestampMixin, table=True):
    """收藏夹封面审核记录表（同一收藏夹同一时刻至多一条 pending，服务层保证）。"""

    __tablename__ = "TFolderCoverAudit"
    __table_args__ = (
        PrimaryKeyConstraint("pk", name="TFolderCoverAudit_pkey"),
        Index(
            "idx_folder_cover_audit_status_created",
            "auditStatus",
            text("created_at DESC"),
        ),
        Index("idx_folder_cover_audit_folder_status", "folderId", "auditStatus"),
        {"extend_existing": True, "comment": "收藏夹封面审核：pending/approved/rejected，通过后写入 cover_url"},
    )

    pk: int = Field(default=None, primary_key=True, sa_type=BIGINT, sa_column_kwargs={"autoincrement": True})
    folderId: int = Field(default=None, nullable=False, sa_type=BIGINT, description="所属收藏夹 id（雪花 ID，仅存 ID 不建跨表 FK）")
    # mid 仅存 BIGINT，不建跨库 FK（用户主数据在 pptr Postgres）
    mid: int = Field(default=None, nullable=False, sa_type=BIGINT, description="提交封面的用户 UID")
    oldCover: str | None = Field(default=None, max_length=1024, description="提交时的旧封面 URL（用于对比/追溯）")
    newCover: str = Field(default=None, nullable=False, max_length=1024, description="申请的新封面 URL")
    auditStatus: FolderCoverAuditStatusEnum = Field(
        default=FolderCoverAuditStatusEnum.PENDING,
        sa_type=SAEnum(FolderCoverAuditStatusEnum),
        description="审核状态：pending/approved/rejected",
    )
    auditOperatorMid: int | None = Field(default=None, sa_type=BIGINT, description="审核人 MID（admin）")
    auditReason: str | None = Field(default=None, max_length=500, description="驳回原因 / 备注（驳回时填写）")
    auditedAt: datetime | None = Field(default=None, description="审核时间")


__all__ = ["TFolderCoverAudit"]
