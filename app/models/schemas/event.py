"""事件提醒模块的请求 / 响应模型。"""

from datetime import datetime

from sqlmodel import Field, SQLModel

from app.models.enums import EventTypeEnum, SourceTypeEnum
from app.models.schemas.base import AutoStrMixin


class EventReportReq(SQLModel, AutoStrMixin):
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
        description="业务资源id（如评论rpid / 动态dynId）：与 source_type(bizType) 共同唯一定位原资源供前端跳转；同时参与幂等键计算，为空时同一人对同一实体的同类行为只记一条",
    )


class EventReportResp(SQLModel, AutoStrMixin):
    accepted: bool = Field(default=True, description="是否已受理")
    event_id: int | None = Field(default=None, description="落库后的事件id")
    duplicated: bool = Field(default=False, description="是否命中幂等被去重")


class EventItem(SQLModel, AutoStrMixin):
    """明细列表中的一条事件。"""

    id: int
    event_type: EventTypeEnum
    source_type: SourceTypeEnum
    source_id: str
    biz_id: str | None = Field(
        default=None, description="业务资源id（如评论rpid），与 source_type 共同唯一定位原资源"
    )
    source_title: str | None = None
    source_cover: str | None = None
    actor_mid: int
    actor_name: str | None = None
    actor_avatar: str | None = None
    content: str | None = None
    jump_url: str | None = None
    is_read: bool = False
    created_at: datetime


class EventActorBrief(SQLModel, AutoStrMixin):
    """聚合卡片上展示的触发者头像信息。"""

    actor_mid: int
    actor_name: str | None = None
    actor_avatar: str | None = None


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


class EventAggregateResp(SQLModel, AutoStrMixin):
    items: list[EventAggregateItem] = Field(default_factory=list)
    total: int = Field(default=0, description="聚合分组总数")
    page_num: int = 1
    page_size: int = 20


class EventUserBrief(SQLModel, AutoStrMixin):
    """聚合条目中的一位触发者（对齐 B 站 msgfeed 的 users[]）。

    后端单条最多返回 4 个触发者（按触发时间倒序去重），多余的不返回；
    前端按 B 站样式展示（左侧最多 2 个头像堆叠 + 等N人文案）。
    """

    mid: int
    nickname: str | None = None
    avatar: str | None = None
    fans: int = 0
    follow: bool = Field(
        default=False, description="接收者是否已关注该触发者（供通知卡片关注按钮使用）"
    )


class EventMsgfeedContent(SQLModel, AutoStrMixin):
    """聚合条目中的内容实体（对齐 B 站 msgfeed 的 item）。

    评论关系字段（subject_id / root_id / source_id / target_id / 三段正文 / like_state）
    不冗余存储，读取时按 biz_id（rpid）实时回捞评论表补全（见 Phase L2）。
    仅保留对本项目 Web 前端有信息量的字段（见 Phase L1 裁剪判断）。
    """

    item_id: int = 0
    type: str = ""
    business: str = ""
    biz_id: str | None = Field(
        default=None,
        description="业务资源id（如评论rpid / 动态dynId），与 business(bizType) 共同唯一定位原资源供前端跳转",
    )
    # ---- 评论树关系（对齐 B 站 reply 通知 item，雪花 id 一律字符串出参）----
    subject_id: str = Field(
        default="", description="评论区主体id（oid，即视频/动态id，字符串出参）"
    )
    root_id: str = Field(default="", description="根评论rpid；空串表示一级评论")
    source_id: str = Field(
        default="", description="触发者评论rpid（= biz_id，即触发者的回复）"
    )
    target_id: str = Field(
        default="", description="被回复的评论rpid（root_id 的楼中楼 target）"
    )
    title: str | None = None
    desc: str | None = None
    image: str | None = None
    uri: str | None = None
    # ---- 评论正文（读取时按 rpid 实时回捞，不冗余存储）----
    root_reply_content: str = Field(default="", description="根评论正文")
    source_content: str = Field(default="", description="触发者评论正文（他回复你时写的内容）")
    target_reply_content: str = Field(default="", description="被回复评论正文")
    # ---- 互动状态 ----
    like_state: int = Field(
        default=0, description="接收者对触发者评论的点赞态：0无 / 1赞 / 2踩"
    )
    ctime: int = 0


class EventMsgfeedItem(SQLModel, AutoStrMixin):
    """按内容聚合的一条记录（对齐 B 站 msgfeed total.items[]）。"""

    id: int = 0
    users: list[EventUserBrief] = Field(default_factory=list)
    item: EventMsgfeedContent = Field(default_factory=EventMsgfeedContent)
    counts: int = 0
    like_time: datetime | None = None
    notice_state: int = 0


class EventMsgfeedCursor(SQLModel, AutoStrMixin):
    """分页游标（对齐 B 站 msgfeed total.cursor）。"""

    is_end: bool = False
    id: int | None = None
    time: datetime | None = None


class EventMsgfeedSection(SQLModel, AutoStrMixin):
    """latest / total 共用的区块结构。"""

    cursor: EventMsgfeedCursor | None = None
    items: list[EventMsgfeedItem] = Field(default_factory=list)


class EventListResp(SQLModel, AutoStrMixin):
    """互动提醒列表（对齐 B 站 x/msgfeed/* 聚合结构）。

    - `latest`：最新若干条（含 cursor.last_view_at 语义，此处 cursor 复用为时间游标）；
    - `total`：完整分页列表，`cursor` 用于下一页翻页（id 游标 + time 时间游标）。
    """

    latest: EventMsgfeedSection = Field(default_factory=EventMsgfeedSection)
    total: EventMsgfeedSection = Field(default_factory=EventMsgfeedSection)


class EventReadReq(SQLModel, AutoStrMixin):
    """已读请求：三种粒度任选其一。"""

    event_ids: list[int] | None = Field(default=None, description="按事件id精确已读")
    event_type: EventTypeEnum | None = Field(
        default=None, description="按类型一键已读（配合 source 可缩小到单个聚合分组）"
    )
    source_type: SourceTypeEnum | None = Field(default=None, description="按来源类型已读")
    source_id: str | None = Field(default=None, max_length=64, description="按来源id已读")


class EventReadResp(SQLModel, AutoStrMixin):
    affected: int = 0
    unread_count: int = 0


class EventUnreadResp(SQLModel, AutoStrMixin):
    """各类型未读数汇总（前端红点）。"""

    like: int = 0
    reply: int = 0
    at: int = 0
    notify: int = 0
    dm: int = 0
    total: int = 0


__all__ = [
    "EventActorBrief",
    "EventAggregateItem",
    "EventAggregateResp",
    "EventItem",
    "EventListResp",
    "EventReadReq",
    "EventReadResp",
    "EventReportReq",
    "EventReportResp",
    "EventUnreadResp",
]
