"""系统通知模块的请求 / 响应模型。"""

import json
from datetime import datetime, timezone

from sqlmodel import Field, SQLModel

from app.models.enums import NotifyLevelEnum, NotifyStatusEnum, NotifyTargetTypeEnum


class NotifyCreateReq(SQLModel):
    """管理员发布通知的请求体。"""

    title: str = Field(min_length=1, max_length=256, description="通知标题")
    content: str = Field(min_length=1, description="通知正文")
    jump_url: str | None = Field(default=None, max_length=512, description="跳转链接")
    target_type: NotifyTargetTypeEnum = Field(
        default=NotifyTargetTypeEnum.ALL, description="目标用户类型"
    )
    target_value: str | None = Field(
        default=None,
        max_length=2048,
        description="目标值：role 填角色名；level 填最低等级；custom 填逗号分隔的 mid 列表",
    )
    level: NotifyLevelEnum = Field(
        default=NotifyLevelEnum.NORMAL, description="通知重要级别"
    )
    publish_at: datetime | None = Field(
        default=None, description="定时发布时间，为空表示立即发布"
    )
    expire_at: datetime | None = Field(default=None, description="过期时间")
    publish_now: bool = Field(
        default=True, description="是否直接进入已发布状态（False 则存为草稿）"
    )


class NotifyUpdateReq(SQLModel):
    """管理员修改通知（仅草稿态可改内容）。"""

    title: str | None = Field(default=None, max_length=256)
    content: str | None = Field(default=None)
    jump_url: str | None = Field(default=None, max_length=512)
    target_type: NotifyTargetTypeEnum | None = Field(default=None)
    target_value: str | None = Field(default=None, max_length=2048)
    level: NotifyLevelEnum | None = Field(default=None)
    publish_at: datetime | None = Field(default=None)
    expire_at: datetime | None = Field(default=None)
    status: NotifyStatusEnum | None = Field(default=None)


class NotifyItem(SQLModel):
    """用户侧看到的一条系统通知。"""

    id: int
    title: str
    content: str
    jump_url: str | None = None
    level: NotifyLevelEnum = NotifyLevelEnum.NORMAL
    publish_at: datetime
    expire_at: datetime | None = None
    is_read: bool = False
    read_at: datetime | None = None
    dispatched: bool = False


class NotifyAdminItem(SQLModel):
    """管理员侧看到的通知（含投放配置与状态）。"""

    id: int
    title: str
    content: str
    jump_url: str | None = None
    target_type: NotifyTargetTypeEnum
    target_value: str | None = None
    level: NotifyLevelEnum
    status: NotifyStatusEnum
    publish_at: datetime
    expire_at: datetime | None = None
    creator_mid: int
    dispatched: bool = False
    created_at: datetime


class NotifyPullResp(SQLModel):
    """定时拉取通知的响应。

    `cursor` 为本次拉取后的新游标，客户端下次带上即可只拿增量；
    服务端同时会持久化该游标，双保险避免重复消费。
    """

    items: list[NotifyItem] = Field(default_factory=list)
    cursor: int = Field(default=0, description="本次拉取后的最大通知id")
    unread_count: int = Field(default=0, description="当前未读总数")
    has_more: bool = Field(default=False, description="是否还有更多增量")


class NotifyListResp(SQLModel):
    """用户侧分页查看历史通知。"""

    items: list[NotifyItem] = Field(default_factory=list)
    total: int = 0
    page_num: int = 1
    page_size: int = 20


class NotifyAdminListResp(SQLModel):
    """管理员侧分页查看通知（含投放配置）。"""

    items: list[NotifyAdminItem] = Field(default_factory=list)
    total: int = 0
    page_num: int = 1
    page_size: int = 20


class NotifyReadReq(SQLModel):
    """标记已读请求：传 ids 精确标记，不传则全部标记为已读。"""

    notify_ids: list[int] | None = Field(default=None, description="要标记的通知id列表")


class NotifyReadResp(SQLModel):
    affected: int = Field(default=0, description="受影响条数")
    unread_count: int = Field(default=0, description="操作后的未读数")


# ==================== 模仿 B 站系统通知接口 ====================


class SystemNotifySource(SQLModel):
    """B 站系统通知的 source 字段（官方账号头像 / 名称）。"""

    name: str = ""
    logo: str = ""


class SystemNotifyItem(SQLModel):
    """模仿 B 站 `/x/v2/feedsystem/system_notify/get` 的列表项。

    - `cursor`：发布时间（UTC）的纳秒时间戳，与 B 站一致。
    - `content`：B 站把正文包成 `{"web": "..."}` 的 JSON 字符串。
    - `type`：系统通知固定为 4。
    - `is_send`：映射本服务的 `dispatched`（是否已投递推送）。
    """

    id: int
    cursor: int = 0
    type: int = 4
    title: str
    content: str
    source: SystemNotifySource = Field(default_factory=SystemNotifySource)
    time_at: str = ""
    card_type: int = 0
    card_brief: str = ""
    card_msg_brief: str = ""
    card_cover: str = ""
    card_story_title: str = ""
    card_link: str = ""
    mc: str = ""
    is_station: int = 1
    is_send: int = 0
    notify_cursor: int = 0

    @classmethod
    def from_notify(cls, item: "NotifyItem") -> "SystemNotifyItem":
        publish_at = item.publish_at
        if publish_at.tzinfo is None:
            publish_at = publish_at.replace(tzinfo=timezone.utc)
        cursor = int(publish_at.timestamp() * 1_000_000_000)
        content = json.dumps({"web": item.content}, ensure_ascii=False)
        return cls(
            id=item.id,
            cursor=cursor,
            title=item.title,
            content=content,
            time_at=item.publish_at.strftime("%Y-%m-%d %H:%M:%S"),
            card_type=1 if item.jump_url else 0,
            card_link=item.jump_url or "",
            is_send=1 if item.dispatched else 0,
        )


class SystemNotifyListResp(SQLModel):
    """B 站系统通知列表的 data 体。"""

    system_notify_list: list[SystemNotifyItem] = Field(default_factory=list)


class BiliSystemNotifyResp(SQLModel):
    """模仿 B 站系统通知接口的完整响应包（含 code/msg/message/ttl 外壳）。"""

    code: int = 0
    msg: str = "0"
    message: str = "0"
    ttl: int = 1
    data: SystemNotifyListResp


__all__ = [
    "NotifyCreateReq",
    "NotifyUpdateReq",
    "NotifyItem",
    "NotifyAdminItem",
    "NotifyPullResp",
    "NotifyListResp",
    "NotifyAdminListResp",
    "NotifyReadReq",
    "NotifyReadResp",
    "SystemNotifySource",
    "SystemNotifyItem",
    "SystemNotifyListResp",
    "BiliSystemNotifyResp",
]
