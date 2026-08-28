"""事件提醒（点赞 / 回复 / @提及 等互动）相关 Pydantic / SQLModel 响应模型。

设计原则（与 Phase L2 一致）：
- **触发者只存 mid**：昵称 / 头像不冗余上报、不冗余落库，读取时按 mid 回查用户服务补全；
- **原资源只存 id**：`source_id` / `source_type` / `biz_id` 定位原资源，
  标题 / 封面 / 跳转链接（即 `title` / `image` / `uri`）读取时实时回捞原资源补全，
  不在事件表冗余存储快照，省空间也更不易过期；
- `content` / `desc` 仅承载事件自身正文（回复正文 / @上下文 / 审核驳回原因），与原资源区分。
"""

from datetime import datetime
from typing import Optional

from sqlmodel import Field, SQLModel

from app.models.enums import EventTypeEnum, SourceTypeEnum
from app.models.schemas.base import AutoStrMixin


class EventUserBrief(SQLModel):
    """事件触发者 / 互动用户简况。

    仅含 mid + 读取时回查得到的昵称 / 头像 / 粉丝数 / 关注态，
    不冗余携带触发者 name / avatar 快照。
    """

    mid: int
    nickname: str | None = None
    avatar: str | None = None
    fans: int = 0
    follow: bool = False


class EventReportReq(SQLModel, AutoStrMixin):
    """上报一条用户行为事件（由业务方 / 爬虫服务调用）。

    仅上报「定位所需的 id + 事件自身的正文」：

    - `actor_mid` 触发者 mid（一切以 mid 为准，昵称 / 头像读取时按 mid 回查用户服务，不冗余上报 name / avatar）；
    - `source_id` / `source_type` / `biz_id` 定位原资源，标题 / 封面 / 跳转链接等**读取时实时回捞原资源补全**，不冗余上报；
    - `content` 仅承载事件自身正文（如回复正文 / @上下文 / 审核驳回原因），与原资源快照区分。
    """

    mid: int = Field(description="接收提醒的用户mid")
    event_type: EventTypeEnum = Field(description="事件类型：like / reply / at")
    source_type: SourceTypeEnum = Field(
        default=SourceTypeEnum.OTHER, description="来源实体类型"
    )
    source_id: str = Field(min_length=1, max_length=64, description="来源实体id")
    actor_mid: int = Field(description="触发行为的用户mid")
    content: str | None = Field(default=None, description="回复正文 / @上下文")
    biz_id: str | None = Field(
        default=None,
        max_length=64,
        description="业务资源id（如评论rpid / 动态dynId）：与 source_type 共同唯一定位原资源供前端跳转；同时参与幂等键计算，为空时同一人对同一实体的同类行为只记一条",
    )


class EventReportResp(SQLModel, AutoStrMixin):
    """上报结果。"""

    accepted: bool = True
    event_id: int | None = None
    duplicated: bool = False


class EventItem(SQLModel, AutoStrMixin):
    """明细列表中的一条事件。

    `title` / `image` / `uri` 读取时按 source_id 实时回捞原资源补全（见 Phase L2），不冗余存储；
    触发者仅保留 `actor_mid`，昵称 / 头像读取时按 mid 回查用户服务。
    """

    id: int
    event_type: EventTypeEnum
    source_type: SourceTypeEnum
    source_id: str
    biz_id: str | None = Field(
        default=None, description="业务资源id（如评论rpid），与 source_type 共同唯一定位原资源"
    )
    title: str | None = None
    image: str | None = None
    uri: str | None = None
    actor_mid: int
    desc: str | None = None
    is_read: bool = False
    created_at: datetime


class EventAggregateItem(SQLModel, AutoStrMixin):
    """按 source_type + source_id 聚合后的一张卡片。

    例如「张三、李四等 12 人赞了你的动态」，
    对应 count=12、actors 取最近 3 位、latest_* 取最新一条。
    """

    event_type: EventTypeEnum
    source_type: SourceTypeEnum
    source_id: str
    biz_id: str | None = Field(
        default=None,
        description="组内最新一条事件的业务资源id（如评论rpid），与 source_type 共同唯一定位原资源供前端跳转",
    )
    title: str | None = None
    image: str | None = None
    uri: str | None = None
    count: int = Field(default=0, description="该分组下的事件总数")
    unread_count: int = Field(default=0, description="该分组下的未读数")
    actors: list[EventUserBrief] = Field(
        default_factory=list, description="最近的若干触发者（用于头像堆叠展示），仅含 mid，昵称 / 头像读取时回查"
    )
    latest_event_id: int = Field(default=0, description="分组内最新事件id")
    latest_desc: str | None = Field(default=None, description="分组内最新事件内容")
    latest_at: datetime | None = Field(default=None, description="分组内最新事件时间")


class EventMsgfeedContent(SQLModel, AutoStrMixin):
    """聚合条目中的内容实体（对齐 B 站 msgfeed 的 item）。

    评论层级关系（root_id / source_id / target_id）与正文（source_content /
    target_content）读取时按 source_id（rpid）实时回捞评论表补全（见 Phase L2），
    不冗余存储，仅靠 resource_id + resource_type 唯一定位原资源。

    `title` / `desc` / `image` / `uri` 同样读取时按 source_type + source_id 实时回捞
    原资源补全，不冗余存储快照。
    """

    item_id: int
    type: int
    business: int = 0
    resource_type: int = 0
    resource_id: str = ""
    root_id: str = ""
    source_id: str = ""
    target_id: str = ""
    title: str = ""
    desc: str = ""
    image: str = ""
    uri: str = ""
    source_content: str = ""
    target_content: str = ""
    ctime: int


class EventMsgfeedItem(SQLModel, AutoStrMixin):
    """msgfeed 聚合列表中的一条（users + item）。"""

    id: int
    users: list[EventUserBrief]
    item: EventMsgfeedContent
    counts: int = 0
    notice_state: int = 0


class EventMsgfeedSection(SQLModel, AutoStrMixin):
    """msgfeed 分段（latest / total）。"""

    cursor: "EventMsgfeedCursor"
    items: list[EventMsgfeedItem]


class EventMsgfeedCursor(SQLModel):
    """msgfeed 翻页游标。"""

    is_end: bool = False
    id: int | None = None
    time: datetime | None = None


class EventListResp(SQLModel, AutoStrMixin):
    """消息中心列表响应：latest 为最新一条，total 为完整分页。"""

    latest: EventMsgfeedSection
    total: EventMsgfeedSection


class EventAggregateResp(SQLModel, AutoStrMixin):
    """聚合卡片列表响应。"""

    items: list[EventAggregateItem]
    total: int
    page_num: int
    page_size: int


class EventReadReq(SQLModel):
    """已读请求（支持 id / 类型 / 聚合分组三种粒度）。"""

    event_ids: list[int] = Field(default_factory=list)
    event_type: EventTypeEnum | None = None
    source_type: SourceTypeEnum | None = None
    source_id: str | None = None


class EventReadResp(SQLModel, AutoStrMixin):
    """已读结果。"""

    affected: int = 0
    unread_count: int = 0


class EventCountResp(SQLModel):
    """未读计数响应。"""

    total: int = 0
    unread: int = 0


class EventUnreadResp(SQLModel):
    """各类型未读计数响应（前端红点）。

    字段与前端 `UnreadSummary`（`MessageLayout.vue`）及 `GET /msg_feed/unread`
    聚合接口保持一致：点赞 / 回复 / @ / 系统通知 / 私信 各模块的未读数 + 总数。
    """

    like: int = Field(default=0, description="点赞未读")
    reply: int = Field(default=0, description="回复未读")
    at: int = Field(default=0, description="@提及未读")
    notify: int = Field(default=0, description="系统通知未读")
    dm: int = Field(default=0, description="私信未读")
    total: int = Field(default=0, description="全站未读总数")


class EventPushPayload(SQLModel, AutoStrMixin):
    """站内信推送载荷（与消息推送子系统对齐）。"""

    event_id: int
    mid: int
    event_type: EventTypeEnum
    source_type: SourceTypeEnum
    source_id: str
    biz_id: str | None = None
    title: str = ""
    desc: str = ""


__all__ = [
    "EventUserBrief",
    "EventReportReq",
    "EventReportResp",
    "EventItem",
    "EventAggregateItem",
    "EventMsgfeedContent",
    "EventMsgfeedItem",
    "EventMsgfeedSection",
    "EventMsgfeedCursor",
    "EventListResp",
    "EventAggregateResp",
    "EventReadReq",
    "EventReadResp",
    "EventCountResp",
    "EventUnreadResp",
    "EventPushPayload",
]
