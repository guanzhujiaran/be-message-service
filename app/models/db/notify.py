"""系统通知相关表模型。

三张表分工：

- `msg_notify`：管理员发布的通知本体，全站只存一份（读扩散）。
  系统通知量级小、受众广，若按用户写扩散会产生 N 倍冗余，
  因此这里采用「一份内容 + 用户侧游标」的读扩散方案。
- `msg_notify_cursor`：每个用户的拉取游标，定时拉取时只捞
  `id > last_notify_id` 的增量，从根源上避免重复消费。
- `msg_notify_state`：用户对单条通知的已读状态，
  (mid, notify_id) 唯一索引保证并发下的幂等。
"""

from datetime import datetime

from sqlalchemy import BIGINT, Text
from sqlmodel import Column, Field, SQLModel, UniqueConstraint

from app.models.db.base import TimestampMixin, str_enum_type
from app.models.enums import NotifyLevelEnum, NotifyStatusEnum, NotifyTargetTypeEnum


class NotifyMessage(TimestampMixin, table=True):
    """系统通知本体（管理员发布）。"""

    __tablename__ = "msg_notify"
    __table_args__ = {"extend_existing": True}

    id: int | None = Field(default=None, primary_key=True)

    title: str = Field(max_length=256, description="通知标题")
    content: str = Field(sa_column=Column(Text), description="通知正文")
    jump_url: str | None = Field(default=None, max_length=512, description="跳转链接")

    # ---- 目标受众（按用户类型推送）----
    target_type: NotifyTargetTypeEnum = Field(
        default=NotifyTargetTypeEnum.ALL,
        sa_type=str_enum_type(NotifyTargetTypeEnum),
        index=True,
        description="目标用户类型",
    )
    target_value: str | None = Field(
        default=None,
        max_length=2048,
        description="目标值：role=角色名 / level=最低等级 / custom=逗号分隔mid列表",
    )

    level: NotifyLevelEnum = Field(
        default=NotifyLevelEnum.NORMAL,
        sa_type=str_enum_type(NotifyLevelEnum),
        description="通知重要级别",
    )
    status: NotifyStatusEnum = Field(
        default=NotifyStatusEnum.DRAFT,
        sa_type=str_enum_type(NotifyStatusEnum),
        index=True,
        description="通知状态",
    )

    publish_at: datetime = Field(
        default_factory=datetime.now,
        index=True,
        description="生效时间：定时发布用，未到该时间不会被拉取",
    )
    expire_at: datetime | None = Field(
        default=None, description="过期时间，为空表示永不过期"
    )

    creator_mid: int = Field(sa_type=BIGINT, index=True, description="发布者mid")
    # 由定时任务标记：该通知是否已完成一轮推送投递，避免重复推送
    dispatched: bool = Field(default=False, index=True, description="是否已完成推送投递")
    dispatched_at: datetime | None = Field(default=None, description="推送投递完成时间")


class NotifyCursor(TimestampMixin, table=True):
    """用户系统通知拉取游标（避免重复消费的核心）。"""

    __tablename__ = "msg_notify_cursor"
    __table_args__ = (
        UniqueConstraint("mid", name="uq_notify_cursor_mid"),
        {"extend_existing": True},
    )

    id: int | None = Field(default=None, primary_key=True)
    mid: int = Field(sa_type=BIGINT, index=True, description="用户mid")
    last_notify_id: int = Field(
        default=0, description="已拉取到的最大通知id，下次只捞更大的id"
    )
    last_pull_at: datetime | None = Field(default=None, description="上次拉取时间")


class NotifyState(TimestampMixin, table=True):
    """用户对单条系统通知的已读状态。"""

    __tablename__ = "msg_notify_state"
    __table_args__ = (
        UniqueConstraint("mid", "notify_id", name="uq_notify_state_mid_notify"),
        {"extend_existing": True},
    )

    id: int | None = Field(default=None, primary_key=True)
    mid: int = Field(sa_type=BIGINT, index=True, description="用户mid")
    notify_id: int = Field(index=True, description="通知id")
    is_read: bool = Field(default=False, index=True, description="是否已读")
    read_at: datetime | None = Field(default=None, description="已读时间")
    # 用户可单独删除某条通知（仅自己不可见）
    is_deleted: bool = Field(default=False, description="用户是否已删除该通知")


__all__ = ["NotifyMessage", "NotifyCursor", "NotifyState", "SQLModel"]
