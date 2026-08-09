"""消息设置与用户活跃度表模型。

`msg_user_setting` 是所有推送动作的第一道闸门：任何提醒在落库 / 推送前
都会先查这里，用户关掉的类型直接标记为 skipped，不产生推送流量。

`msg_user_activity` 记录用户最近一次行为时间，是推送策略分流的依据：
窗口内活跃 → 实时推送；否则进入批量队列，由定时任务聚合后一次性推送，
既降低小设备的瞬时压力，也避免对沉默用户的高频打扰。
"""

from datetime import datetime

from sqlalchemy import BIGINT
from sqlmodel import Field, SQLModel, UniqueConstraint

from app.models.db.base import TimestampMixin


class UserMessageSetting(TimestampMixin, table=True):
    """用户消息设置（可自定义接收哪些提醒）。"""

    __tablename__ = "msg_user_setting"
    __table_args__ = (
        UniqueConstraint("mid", name="uq_user_setting_mid"),
        {"extend_existing": True},
    )

    id: int | None = Field(default=None, primary_key=True)
    mid: int = Field(sa_type=BIGINT, index=True, description="用户mid")

    # ---- 事件提醒开关 ----
    recv_like: bool = Field(default=True, description="是否接收点赞提醒")
    recv_reply: bool = Field(default=True, description="是否接收回复提醒")
    recv_at: bool = Field(default=True, description="是否接收@提醒")

    # ---- 私信开关 ----
    recv_stranger_dm: bool = Field(default=True, description="是否接收陌生人私信")

    # ---- 系统通知开关 ----
    recv_notify: bool = Field(default=True, description="是否接收系统通知")

    # ---- 外部推送渠道（PushMe / 邮件等）总开关 ----
    push_enabled: bool = Field(default=True, description="是否推送到外部渠道")
    # 免打扰时段（0~23 小时），为空表示不限制
    dnd_start_hour: int | None = Field(default=None, description="免打扰开始小时")
    dnd_end_hour: int | None = Field(default=None, description="免打扰结束小时")


class UserActivity(TimestampMixin, table=True):
    """用户活跃度（推送策略分流依据）。"""

    __tablename__ = "msg_user_activity"
    __table_args__ = (
        UniqueConstraint("mid", name="uq_user_activity_mid"),
        {"extend_existing": True},
    )

    id: int | None = Field(default=None, primary_key=True)
    mid: int = Field(sa_type=BIGINT, index=True, description="用户mid")
    last_active_at: datetime = Field(
        default_factory=datetime.now, index=True, description="最近一次活跃时间"
    )
    active_count: int = Field(default=0, description="累计活跃次数")
    # 与本地库 msg_user_activity 实际结构对齐：待推送事件计数（NOT NULL 无默认）
    pending_push_count: int = Field(default=0, index=True, description="待推送事件计数")


__all__ = ["UserMessageSetting", "UserActivity", "SQLModel"]
