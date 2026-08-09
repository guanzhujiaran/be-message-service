"""消息设置模块的请求 / 响应模型。"""

from datetime import datetime

from sqlmodel import Field, SQLModel


class MessageSettingResp(SQLModel):
    """用户当前的消息设置。"""

    mid: int
    recv_like: bool = True
    recv_reply: bool = True
    recv_at: bool = True
    recv_stranger_dm: bool = True
    recv_notify: bool = True
    push_enabled: bool = True
    dnd_start_hour: int | None = None
    dnd_end_hour: int | None = None
    updated_at: datetime | None = None


class MessageSettingUpdateReq(SQLModel):
    """更新消息设置：只传需要改的字段，未传字段保持原值。"""

    recv_like: bool | None = Field(default=None, description="是否接收点赞提醒")
    recv_reply: bool | None = Field(default=None, description="是否接收回复提醒")
    recv_at: bool | None = Field(default=None, description="是否接收@提醒")
    recv_stranger_dm: bool | None = Field(default=None, description="是否接收陌生人私信")
    recv_notify: bool | None = Field(default=None, description="是否接收系统通知")
    push_enabled: bool | None = Field(default=None, description="是否推送到外部渠道")
    dnd_start_hour: int | None = Field(
        default=None, ge=0, le=23, description="免打扰开始小时"
    )
    dnd_end_hour: int | None = Field(
        default=None, ge=0, le=23, description="免打扰结束小时"
    )


class UserActivityResp(SQLModel):
    """用户活跃度快照（当前是否在线，用于前端轮询节奏）。"""

    mid: int
    last_active_at: datetime
    active_count: int = 0
    is_active: bool = Field(default=False, description="当前是否处于活跃窗口内")


__all__ = ["MessageSettingResp", "MessageSettingUpdateReq", "UserActivityResp"]
