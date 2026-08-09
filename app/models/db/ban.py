"""用户封禁记录表（审核联动）。

审核评论 / 私信时，管理员可对违规用户执行封禁，禁止其继续使用对应服务
（评论区 / 私信）。封禁数据自包含在 be-message-service（落 `msg_user_ban`），
**不回写 pptr**，与用户主数据解耦。

设计要点：

- 一条记录对应「一个用户在某几个服务上的封禁」；重复封禁同一用户同一服务时
  以最新记录为准（写入层做 upsert 语义，读取层取最新生效记录）。
- `banned_until` 为限时封禁的解封时间；`permanent` 时为 `None`，读取层据此判定永久。
- 读取层（见 `app.services.ban_service`）实时计算是否仍在封禁期内，
  限时封禁到期即视为失效，无需定时任务翻转状态。
"""

from datetime import datetime

from sqlalchemy import BIGINT, Column, JSON, Index

from sqlmodel import Field, SQLModel

from app.models.db.base import TimestampMixin, str_enum_type
from app.models.enums import BanDurationTypeEnum, BanServiceEnum, BanStatusEnum


class UserBan(TimestampMixin, table=True):
    """用户封禁记录（按用户 + 服务维度）。"""

    __tablename__ = "msg_user_ban"
    __table_args__ = (
        # 按用户 + 状态筛出生效记录（封禁判定高频）
        Index("idx_user_ban_mid_status", "mid", "status"),
        # 按状态 + 创建时间翻页（封禁记录列表）
        Index("idx_user_ban_status_created", "status", "created_at"),
        {"extend_existing": True},
    )

    id: int | None = Field(default=None, primary_key=True)

    # 被封禁用户 mid
    mid: int = Field(sa_type=BIGINT, index=True, description="被封禁用户 mid")

    # 封禁的服务范围（comment / dm），JSON 数组
    ban_services: list[str] = Field(
        sa_column=Column(JSON()),
        description="封禁的服务范围：comment 评论 / dm 私信",
    )

    # 封禁理由（必填，管理端展示与申诉依据）
    reason: str = Field(max_length=512, description="封禁理由")

    # 时长类型：temporary 限时 / permanent 永久
    duration_type: BanDurationTypeEnum = Field(
        sa_type=str_enum_type(BanDurationTypeEnum),
        description="封禁时长类型：temporary 限时 / permanent 永久",
    )
    # 限时封禁天数（temporary 时必填，>=1）；permanent 时为 None
    duration_days: int | None = Field(
        default=None, description="限时封禁天数（temporary 时有效）"
    )
    # 计算出的解封时间（permanent 时为 None）；读取层据此判定是否到期
    banned_until: datetime | None = Field(
        default=None, description="解封时间（permanent 为 None 表示永久）"
    )

    # 操作管理员 mid
    operator_mid: int = Field(sa_type=BIGINT, description="操作管理员 mid")
    # 记录状态：active 生效中 / lifted 已解封
    status: BanStatusEnum = Field(
        default=BanStatusEnum.ACTIVE,
        sa_type=str_enum_type(BanStatusEnum),
        index=True,
        description="封禁状态：active 生效中 / lifted 已解封",
    )


__all__ = ["UserBan", "SQLModel"]
