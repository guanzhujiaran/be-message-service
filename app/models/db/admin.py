"""消息管理端（评论 / 私信审核）细粒度权限表。

授权数据自包含在 be-message-service，与 RPA 权限体系解耦：
- 仅 root 管理员可授权 / 撤销其他用户的消息管理端权限；
- `permissions` 为细粒度权限列表（与 `bili_common.deps.permissions.UserPermission` 同一套词表），
  root 专属权限（查看内容明文 / 设置过审没过审）不可写入本表（落库前 sanitize）。
"""

from sqlalchemy import BIGINT, Column, JSON, UniqueConstraint

from sqlmodel import Field, SQLModel

from app.models.db.base import TimestampMixin


class MessageAdmin(TimestampMixin, table=True):
    """消息管理端管理员（非 root）。"""

    __tablename__ = "msg_admin"
    __table_args__ = (
        UniqueConstraint("mid", name="uq_msg_admin_mid"),
        {"extend_existing": True},
    )

    id: int | None = Field(default=None, primary_key=True)

    # 被授予管理权限的用户 mid（唯一，一个用户至多一条授权记录）
    mid: int = Field(sa_type=BIGINT, index=True, description="被授予权限的用户 mid")
    # 授权者 mid（应为 root）
    granted_by: int = Field(sa_type=BIGINT, description="授权者 mid（root）")
    # 细粒度权限列表，root 专属权限不可授予
    permissions: list[str] = Field(
        default_factory=list,
        sa_column=Column(JSON),
        description="细粒度权限列表（comment:view-queue / dm:view-queue 等，root 专属权限不可授予）",
    )
    note: str | None = Field(default=None, max_length=512, description="备注")


__all__ = ["MessageAdmin", "SQLModel"]
