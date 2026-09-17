"""事件提醒（点赞 / 回复 / @提及 等互动）相关 Pydantic / SQLModel 响应模型。

设计原则（与 Phase L2 一致）：
- **触发者只存 mid**：昵称 / 头像不冗余上报、不冗余落库，读取时按 mid 回查用户服务补全；
- **原资源只存 id**：`source_id` / `source_type` / `biz_id` 定位原资源，
  标题 / 封面（即 `title` / `image`）读取时实时回捞原资源补全，
  不在事件表冗余存储快照，省空间也更不易过期；
- **跳转 uri 不在此拼接**：业务由 `business` + `type` 表达，uri 由前端按这两个字段
  自行决定；`business_name` 作为 computed field 给出 `business` 的文字名称；
- `content` / `desc` 仅承载事件自身正文（回复正文 / @上下文 / 审核驳回原因），与原资源区分。
"""

from datetime import datetime
from typing import Optional

from pydantic import computed_field
from sqlmodel import Field, SQLModel

from app.models.biz_type import source_type_label
from bili_common.models import InteractionActionTypeEnum, InteractionBizTypeEnum
from app.models.schemas.base import auto_str

# business（= source_type）的文字名称不再在本模块维护：
# 展示名统一由 `app.models.biz_type.source_type_label()` 供给——
# 有对应 biz_type 的（dynamic / lottery / rpa_*）取 biz_type 的展示名，
# 其余无 biz_type 的（仅评论 COMMENT）走 biz_type 模块的兜底表（计划书 §5.9）。
_DEFAULT_BUSINESS_NAME = "其他"


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


@auto_str
class EventReportReq(SQLModel):
    """上报一条用户行为事件（由业务方 / 爬虫服务调用）。

    仅上报「定位所需的 id + 事件自身的正文」：

    - `actor_mid` 触发者 mid（一切以 mid 为准，昵称 / 头像读取时按 mid 回查用户服务，不冗余上报 name / avatar）；
    - `source_id` / `source_type` / `biz_id` 定位原资源，标题 / 封面 / 跳转链接等**读取时实时回捞原资源补全**，不冗余上报；
    - `content` 仅承载事件自身正文（如回复正文 / @上下文 / 审核驳回原因），与原资源快照区分。
    """

    mid: int = Field(description="接收提醒的用户mid")
    event_type: InteractionActionTypeEnum = Field(description="事件类型：like / reply / at")
    source_type: InteractionBizTypeEnum = Field(
        description="来源实体类型（必填：无对应资源时禁止落库）"
    )
    source_id: str = Field(min_length=1, max_length=64, description="来源实体id")
    actor_mid: int = Field(description="触发行为的用户mid")
    content: str | None = Field(default=None, description="回复正文 / @上下文")
    biz_id: str | None = Field(
        default=None,
        max_length=64,
        description="业务资源id（如评论rpid / 动态dynId）：与 source_type 共同唯一定位原资源供前端跳转；同时参与幂等键计算，为空时同一人对同一实体的同类行为只记一条",
    )


@auto_str
class EventReportResp(SQLModel):
    """上报结果。"""

    accepted: bool = True
    event_id: int | None = None
    duplicated: bool = False


@auto_str
class EventItem(SQLModel):
    """明细列表中的一条事件。

    `title` / `image` 读取时按 source_id 实时回捞原资源补全（见 Phase L2），不冗余存储；
    跳转 uri **不在此拼接**，由前端按 business + type 自行决定；
    触发者仅保留 `actor_mid`，昵称 / 头像读取时按 mid 回查用户服务。
    """

    id: int
    event_type: InteractionActionTypeEnum
    source_type: InteractionBizTypeEnum
    source_id: str
    biz_id: str | None = Field(
        default=None, description="业务资源id（如评论rpid），与 source_type 共同唯一定位原资源"
    )
    title: str | None = None
    image: str | None = None
    jump_target: str = ""
    resource_deleted: bool = False
    actor_mid: int
    desc: str | None = None
    is_read: bool = False
    created_at: datetime


@auto_str
class EventAggregateItem(SQLModel):
    """按 source_type + source_id 聚合后的一张卡片。

    例如「张三、李四等 12 人赞了你的动态」，
    对应 count=12、actors 取最近 3 位、latest_* 取最新一条。
    """

    event_type: InteractionActionTypeEnum
    source_type: InteractionBizTypeEnum
    source_id: str
    biz_id: str | None = Field(
        default=None,
        description="组内最新一条事件的业务资源id（如评论rpid），与 source_type 共同唯一定位原资源供前端跳转",
    )
    title: str | None = None
    image: str | None = None
    jump_target: str = ""
    resource_deleted: bool = False
    count: int = Field(default=0, description="该分组下的事件总数")
    unread_count: int = Field(default=0, description="该分组下的未读数")
    actors: list[EventUserBrief] = Field(
        default_factory=list, description="最近的若干触发者（用于头像堆叠展示），仅含 mid，昵称 / 头像读取时回查"
    )
    latest_event_id: int = Field(default=0, description="分组内最新事件id")
    latest_desc: str | None = Field(default=None, description="分组内最新事件内容")
    latest_at: datetime | None = Field(default=None, description="分组内最新事件时间")


@auto_str
class EventMsgfeedContent(SQLModel):
    """聚合条目中的内容实体（对齐 B 站 msgfeed 的 item）。

    评论层级关系（root_id / source_id / target_id）与正文（source_content /
    target_content）、作者（source_mid / target_mid）读取时按 source_id（rpid）
    实时回捞评论补全（见 Phase L2 / §5.12），不冗余存储，仅靠
    resource_id + resource_type 唯一定位原资源；正文与作者统一经
    `CommentBiz.batch_get_resources` 批量回捞（对齐 §5.11 的 Biz 体系）。

    `title` / `desc` / `image` 同样读取时按 source_type + source_id 实时回捞
    原资源补全，不冗余存储快照。

    **跳转 uri 不在此拼接**：`business`（source_type）+ `type`（event_type）已足够
    定位业务，uri 由前端按这两个字段自行决定。`business_name` 是 `business` 的
    文字形式（computed field），供前端直接展示「对动态 / 对评论」的提醒。
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
    source_content: str = ""
    target_content: str = ""
    # 楼层评论作者（经 `CommentBiz.batch_get_resources` 批量回捞评论快照得到 mid，
    # 再由 `list_msgfeed` 的回查块经 `PptrUser.get_many` 批量回查昵称，计划书 §5.12）：
    # `source_mid` / `source_name` = 触发评论（写下 @ / 回复的那一层）作者 mid / 昵称，
    # `target_mid` / `target_name` = 被回复 / 被 @ 的那一层作者 mid / 昵称；
    # 评论不存在或非正常状态时为 0 / 空串，前端降级为「用户{mid}」或跳过作者展示。
    source_mid: int = 0
    target_mid: int = 0
    source_name: str = ""
    target_name: str = ""
    # 触发评论是否处于「非正常状态」（被删 / 未过审 / 驳回 / 下架 / 待审）：
    # True 时 source_content / target_content 为空，前端展示「该评论已被删除」占位。
    comment_deleted: bool = False
    jump_target: str = ""  # 后端下发的跳转目标 route:{name}?{query}（前端只 router.push）
    resource_deleted: bool = False  # 顶层资源是否已删除/不存在（前端展示占位且不跳转）
    ctime: int

    @computed_field  # type: ignore[prop-decorator]
    @property
    def business_name(self) -> str:
        """`business`（source_type）的文字名称，如「动态」/「评论」/「抽奖」。

        前端据此直接展示这是对哪种实体的提醒，无需自行维护 business→文案 的映射。
        未知 / 缺省 business 统一回落「其他」。
        """
        try:
            st = InteractionBizTypeEnum(self.business)
        except ValueError:
            return _DEFAULT_BUSINESS_NAME
        return source_type_label(st)


@auto_str
class EventMsgfeedItem(SQLModel):
    """msgfeed 聚合列表中的一条（users + item）。"""

    id: int
    users: list[EventUserBrief]
    item: EventMsgfeedContent
    counts: int = 0
    notice_state: int = 0


@auto_str
class EventMsgfeedSection(SQLModel):
    """msgfeed 分段（latest / total）。"""

    cursor: "EventMsgfeedCursor"
    items: list[EventMsgfeedItem]


class EventMsgfeedCursor(SQLModel):
    """msgfeed 翻页游标。"""

    is_end: bool = False
    id: int | None = None
    time: datetime | None = None


@auto_str
class EventListResp(SQLModel):
    """消息中心列表响应：latest 为最新一条，total 为完整分页。

    `total_count` / `unread_count` 用于**对账**——列表按「来源实体」聚合，单页只返回
    `page_size` 张卡片（每张卡片聚合了 N 个用户的同类互动），因此本页 `items` 长度
    天然小于「未读事件数」。这两个字段给出真实总量，避免前端把「单页 20 张」误判为
    「全部只有 20 条 / 已结束」：
    - `total_count`：当前筛选条件下聚合卡片（分组）总数，决定总页数；
    - `unread_count`：当前 `event_type` 下未读事件总数，与 `GET /unread` 的对应字段一致。
    """

    latest: EventMsgfeedSection
    total: EventMsgfeedSection
    total_count: int = Field(
        default=0, description="当前筛选条件下聚合卡片（分组）总数（与单页 items 长度无关）"
    )
    unread_count: int = Field(
        default=0, description="当前 event_type 下未读事件总数，对齐 GET /unread 的对应字段"
    )


@auto_str
class EventAggregateResp(SQLModel):
    """聚合卡片列表响应。"""

    items: list[EventAggregateItem]
    total: int
    page_num: int
    page_size: int


class EventReadReq(SQLModel):
    """已读请求（支持 id / 类型 / 聚合分组 / 时间戳四种粒度）。

    - ``event_ids``：精确已读；
    - ``event_type`` + 可选 ``source_type`` + ``source_id``：按类型 / 聚合分组一键已读；
    - ``read_before``：把该时间戳（含）之前、归属当前用户的互动提醒全部置为已读，
      用于「打开列表即自动已读」——前端在拉取列表后携带调用时刻调用，
      即可把本次请求之前的点赞 / 回复 / @ 消息全部标记已读，无需手动「全部已读」按钮。
    """

    event_ids: list[int] = Field(default_factory=list)
    event_type: InteractionActionTypeEnum | None = None
    source_type: InteractionBizTypeEnum | None = None
    source_id: str | None = None
    read_before: datetime | None = Field(
        default=None, description="标记该时间戳（含）之前的全部消息为已读；不传则按其它条件标记"
    )


@auto_str
class EventReadResp(SQLModel):
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


@auto_str
class EventPushPayload(SQLModel):
    """站内信推送载荷（与消息推送子系统对齐）。"""

    event_id: int
    mid: int
    event_type: InteractionActionTypeEnum
    source_type: InteractionBizTypeEnum
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
