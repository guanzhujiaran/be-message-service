"""事件提醒模块的请求 / 响应模型。"""

from datetime import datetime

from sqlmodel import Field, SQLModel

from app.models.enums import EventTypeEnum, SourceTypeEnum


class EventReportReq(SQLModel):
    """上报一条用户行为事件（由业务方 / 爬虫服务调用）。"""

    mid: int = Field(description="接收提醒的用户mid")
    event_type: EventTypeEnum = Field(description="事件类型：like / reply / at")
    source_type: SourceTypeEnum = Field(
        default=SourceTypeEnum.OTHER, description="来源实体类型"
    )
    source_id: str = Field(min_length=1, max_length=64, description="来源实体id")
    source_title: str | None = Field(default=None, max_length=256)
    source_cover: str | None = Field(default=None, max_length=512)
    actor_mid: int = Field(description="触发行为的用户mid")
    actor_name: str | None = Field(default=None, max_length=64)
    actor_avatar: str | None = Field(default=None, max_length=512)
    content: str | None = Field(default=None, description="回复正文 / @上下文")
    jump_url: str | None = Field(default=None, max_length=512)
    biz_id: str | None = Field(
        default=None,
        max_length=64,
        description="业务唯一标识（如评论id），参与幂等键计算；为空时同一人对同一实体的同类行为只记一条",
    )


class EventReportResp(SQLModel):
    accepted: bool = Field(default=True, description="是否已受理")
    event_id: int | None = Field(default=None, description="落库后的事件id")
    duplicated: bool = Field(default=False, description="是否命中幂等被去重")


class EventItem(SQLModel):
    """明细列表中的一条事件。"""

    id: int
    event_type: EventTypeEnum
    source_type: SourceTypeEnum
    source_id: str
    source_title: str | None = None
    source_cover: str | None = None
    actor_mid: int
    actor_name: str | None = None
    actor_avatar: str | None = None
    content: str | None = None
    jump_url: str | None = None
    is_read: bool = False
    created_at: datetime


class EventActorBrief(SQLModel):
    """聚合卡片上展示的触发者头像信息。"""

    actor_mid: int
    actor_name: str | None = None
    actor_avatar: str | None = None


class EventAggregateItem(SQLModel):
    """按 source_type + source_id 聚合后的一张卡片。

    例如「张三、李四等 12 人赞了你的动态」，
    对应 count=12、actors 取最近 3 位、latest_* 取最新一条。
    """

    event_type: EventTypeEnum
    source_type: SourceTypeEnum
    source_id: str
    source_title: str | None = None
    source_cover: str | None = None
    jump_url: str | None = None
    count: int = Field(default=0, description="该分组下的事件总数")
    unread_count: int = Field(default=0, description="该分组下的未读数")
    actors: list[EventActorBrief] = Field(
        default_factory=list, description="最近的若干触发者（用于头像堆叠展示）"
    )
    latest_event_id: int = Field(default=0, description="分组内最新事件id")
    latest_content: str | None = Field(default=None, description="分组内最新事件内容")
    latest_at: datetime | None = Field(default=None, description="分组内最新事件时间")


class EventAggregateResp(SQLModel):
    items: list[EventAggregateItem] = Field(default_factory=list)
    total: int = Field(default=0, description="聚合分组总数")
    page_num: int = 1
    page_size: int = 20


class EventListResp(SQLModel):
    items: list[EventItem] = Field(default_factory=list)
    total: int = 0
    page_num: int = 1
    page_size: int = 20


class EventReadReq(SQLModel):
    """已读请求：三种粒度任选其一。"""

    event_ids: list[int] | None = Field(default=None, description="按事件id精确已读")
    event_type: EventTypeEnum | None = Field(
        default=None, description="按类型一键已读（配合 source 可缩小到单个聚合分组）"
    )
    source_type: SourceTypeEnum | None = Field(default=None, description="按来源类型已读")
    source_id: str | None = Field(default=None, max_length=64, description="按来源id已读")


class EventReadResp(SQLModel):
    affected: int = 0
    unread_count: int = 0


class EventUnreadResp(SQLModel):
    """各类型未读数汇总（前端红点）。"""

    like: int = 0
    reply: int = 0
    at: int = 0
    notify: int = 0
    dm: int = 0
    total: int = 0


__all__ = [
    "EventReportReq",
    "EventReportResp",
    "EventItem",
    "EventActorBrief",
    "EventAggregateItem",
    "EventAggregateResp",
    "EventListResp",
    "EventReadReq",
    "EventReadResp",
    "EventUnreadResp",
]
