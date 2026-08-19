"""头像更换审核表 ORM 模型（be-message MySQL 主库 `BiliMessageDB`）。

头像更换流程为「先审后发」：
- 用户提交新头像 → 本表插入一条 `auditStatus=pending` 记录，`TUserDetail.avatar` 保持不变（仍为旧头像）；
- 审核通过 → `auditStatus=approved`，`newAvatar` 被写入 pptr Postgres `TUserDetail.avatar` 对外公开；
- 审核驳回 → `auditStatus=rejected`，保持原头像，`auditReason` 记录驳回原因。

约束（对齐 `app.models.db` 既有规范）：
- 表名沿用 `"T"` 前缀 PascalCase（TUserAvatarAudit）；
- 列名使用 camelCase，Python 属性名与数据库列名完全一致；
- 时间戳用 `TimestampMixin`；
- `mid` 系用户字段仅存 BIGINT，不建跨库外键（用户主数据在 pptr Postgres）。
"""

from datetime import datetime

from sqlalchemy import BIGINT, Index, PrimaryKeyConstraint, text
from sqlmodel import Field

from app.models.db.base import TimestampMixin, str_enum_type
from app.models.enums import AvatarAuditStatusEnum


class TUserAvatarAudit(TimestampMixin, table=True):
    """头像更换审核记录表（每人同一时刻至多一条 pending，服务层保证）。"""

    __tablename__ = "TUserAvatarAudit"
    __table_args__ = (
        PrimaryKeyConstraint("pk", name="TUserAvatarAudit_pkey"),
        Index("idx_avatar_audit_status_created", "auditStatus", text("created_at DESC")),
        Index("idx_avatar_audit_mid_created", "mid", text("created_at DESC")),
        {"extend_existing": True, "comment": "头像更换审核：pending/approved/rejected，通过后写入公开头像"},
    )

    pk: int = Field(default=None, primary_key=True, sa_type=BIGINT, sa_column_kwargs={"autoincrement": True})
    # mid 仅存 BIGINT，不建跨库 FK（用户主数据在 pptr Postgres）
    mid: int = Field(default=None, nullable=False, sa_type=BIGINT, description="申请更换头像的用户 UID")
    oldAvatar: str | None = Field(default=None, max_length=1024, description="提交时的旧头像 URL（用于对比/追溯）")
    newAvatar: str = Field(default=None, nullable=False, max_length=1024, description="申请的新头像 URL")
    auditStatus: AvatarAuditStatusEnum = Field(
        default=AvatarAuditStatusEnum.PENDING,
        sa_type=str_enum_type(AvatarAuditStatusEnum),
        description="审核状态：pending/approved/rejected",
    )
    auditOperatorMid: int | None = Field(default=None, sa_type=BIGINT, description="审核人 MID（admin）")
    auditReason: str | None = Field(default=None, max_length=500, description="驳回原因 / 备注（驳回时填写）")
    auditedAt: datetime | None = Field(default=None, description="审核时间")


__all__ = ["TUserAvatarAudit"]
