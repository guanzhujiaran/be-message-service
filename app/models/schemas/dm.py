"""私信模块的请求 / 响应模型。"""

from datetime import datetime

from sqlmodel import Field, SQLModel

from app.models.enums import (
    ResourceAuditStatusEnum,
    DmMsgStatusEnum,
    DmMsgTypeEnum,
    DmRelationEnum,
    DmSessionTypeEnum,
)
from app.models.schemas.audit import AuditSourceInfo
from app.models.schemas.base import AutoStrMixin
from app.models.schemas.user_brief import UserBriefOut
from app.models.str_int import StrInt


class DmSendReq(SQLModel):
    """发送一条私信。"""

    receiver_mid: StrInt = Field(description="接收者mid（雪花 ID，StrInt 兼容前端 str 传参）")
    content: str = Field(min_length=1, max_length=20000, description="消息内容")
    msg_type: DmMsgTypeEnum = Field(default=DmMsgTypeEnum.TEXT, description="消息类型")
    receiver_name: str | None = Field(
        default=None, max_length=64, description="接收者昵称（用于会话列表展示）"
    )
    receiver_avatar: str | None = Field(default=None, max_length=512)


class DmSendResp(SQLModel):
    msgkey: str = Field(description="消息全局唯一键（字符串形式，避免 JS 精度丢失）")
    session_key: str = Field(description="会话键")
    msg_ts: int = Field(description="消息毫秒时间戳")
    filtered: bool = Field(
        default=False, description="是否因对方关闭陌生人私信而被过滤（不会送达）"
    )
    content_async: bool = Field(
        default=True, description="内容是否走异步落库（False 表示已同步兜底写入）"
    )


class DmSessionItem(SQLModel, AutoStrMixin):
    """会话列表中的一个会话。"""

    talker_mid: int
    talker_name: str | None = None
    talker_avatar: str | None = None
    session_key: str
    last_msgkey: str | None = None
    last_content_preview: str | None = None
    last_msg_ts: int = 0
    last_sender_uid: int | None = None
    unread_count: int = 0
    relation: DmRelationEnum = DmRelationEnum.NORMAL
    session_type: DmSessionTypeEnum = Field(
        default=DmSessionTypeEnum.SINGLE,
        description="会话类型：SINGLE=普通 DM；STRANGER=陌生人私信分类",
    )
    is_top: bool = Field(
        default=False, description="是否置顶（= top_ts != 0，兼容 flag 用法）"
    )
    top_ts: int = Field(
        default=0, description="置顶时间戳(毫秒)，0=未置顶；置顶唯一真相源"
    )
    is_muted: bool = False
    updated_at: datetime | None = None


class DmSessionListResp(SQLModel):
    items: list[DmSessionItem] = Field(default_factory=list)
    total: int = 0
    # 主列表（SINGLE）未读之和：用于顶部私信红点；不含 STRANGER（陌生人分类独立红点）
    unread_total: int = Field(default=0, description="主列表（SINGLE）未读数之和")
    # 陌生人分类聚合：用于「陌生人私信」顶部聚合条的红点与条数
    stranger_unread: int = Field(default=0, description="陌生人分类（STRANGER）未读数之和")
    stranger_total: int = Field(
        default=0, description="陌生人分类（STRANGER）会话总数（用于聚合条 [N 条] 副标题）"
    )
    # 前端聚合条展示前提：开关开启 + 确有 STRANGER 会话，两个条件同时满足才展示
    stranger_dm_intercept_enabled: bool = Field(
        default=False,
        description="当前用户是否开启了「陌生人私信拦截」（recv_stranger_dm=false）",
    )


class DmMessageItem(SQLModel, AutoStrMixin):
    """聊天记录中的一条消息。"""

    msgkey: str
    sender_uid: int
    msg_type: DmMsgTypeEnum = DmMsgTypeEnum.TEXT
    msg_status: DmMsgStatusEnum = DmMsgStatusEnum.NORMAL
    content: str | None = Field(
        default=None, description="正文；撤回后为空，内容分片未就绪时回落为摘要"
    )
    msg_ts: int = 0
    content_ready: bool = Field(
        default=True, description="内容是否已从分片读到（False 表示当前为摘要兜底）"
    )
    created_at: datetime | None = None
    audit_state: ResourceAuditStatusEnum = ResourceAuditStatusEnum.NORMAL
    recalled_at: datetime | None = Field(
        default=None, description="撤回时间（仅撤回后非空）"
    )
    recalled_by: int | None = Field(
        default=None, description="撤回操作者mid（前端据此展示「你/对方撤回了一条消息」）"
    )


class DmMessageListResp(SQLModel):
    """聊天记录（游标翻页）。

    使用 msgkey 作为游标而非 offset：msgkey 单调递增且内嵌时间戳，
    翻页时可直接 `msgkey < cursor` 走索引，深翻页不退化。
    """

    items: list[DmMessageItem] = Field(default_factory=list)
    cursor: str | None = Field(default=None, description="下一页游标（本页最小 msgkey）")
    has_more: bool = False
    talker_mid: int = 0
    session_key: str = ""


class DmDeleteReq(SQLModel):
    """删除消息（仅自己不可见，对方仍可见）。"""

    msgkeys: list[str] = Field(description="要删除的 msgkey 列表")


class DmRecallReq(SQLModel):
    """撤回消息（双方均不可见，仅发送者可操作且受时间窗口限制）。"""

    msgkey: str = Field(description="要撤回的 msgkey")


class DmOperationResp(SQLModel):
    affected: int = 0
    message: str = ""


class DmAckReq(SQLModel):
    """标记会话已读，把未读数清零并抬高已读水位。"""

    talker_mid: StrInt = Field(description="对话方mid（雪花 ID，StrInt 兼容前端 str 传参）")
    ack_msgkey: str | None = Field(
        default=None, description="已读到的最大 msgkey，为空表示全部已读"
    )


class DmSessionDeleteReq(SQLModel):
    talker_mid: StrInt = Field(description="要删除的会话对方mid（雪花 ID，StrInt 兼容前端 str 传参）")


class DmTopReq(SQLModel):
    """会话置顶 / 取消置顶（2.59.0）。"""

    talker_mid: StrInt = Field(description="对话方mid（雪花 ID，StrInt 兼容前端 str 传参）")
    top: bool = Field(description="true=置顶（top_ts=now）；false=取消置顶（top_ts=0）")


class DmTopResp(SQLModel, AutoStrMixin):
    """置顶操作结果。"""

    talker_mid: int = Field(default=0, description="被置顶/取消的会话对方 mid")
    top_ts: int = Field(default=0, description="置顶时间戳(毫秒)；0=未置顶")
    is_top: bool = Field(default=False, description="= top_ts != 0")
    affected: int = Field(default=0, description="受影响会话行数（0=会话不存在或幂等无操作）")


class DmAuditItem(SQLModel, AutoStrMixin):
    """私信审核队列中的一条消息。"""

    msgkey: str = Field(description="消息全局唯一键（字符串）")
    sender_mid: int = Field(description="发送者mid")
    talker_mid: int = Field(description="对话方mid")
    session_key: str = Field(description="会话键：小mid_大mid")
    message: str = Field(default="", description="正文摘要（内容分片未就绪时兜底）")
    msg_type: DmMsgTypeEnum = DmMsgTypeEnum.TEXT
    audit_state: ResourceAuditStatusEnum = ResourceAuditStatusEnum.NORMAL
    msg_ts: int = Field(default=0, description="消息毫秒时间戳")
    content_ready: bool = Field(default=False, description="内容是否已异步落库到分片")
    created_at: datetime | None = None
    source: AuditSourceInfo | None = Field(
        default=None, description="内容来源，管理端可点击直达该会话上下文"
    )
    # 管理端审核视角：使用**私有**简档（含脱敏邮箱 / 经验 / 大会员到期 / 角色）
    sender: UserBriefOut | None = Field(
        default=None, description="发送者信息（管理端：含私有字段），装配时直连 pptr 只读取回"
    )


class DmAuditReq(SQLModel):
    """管理端人工审核 / 上下架。"""

    msgkey: str = Field(description="待处理私信 msgkey（字符串）")
    op: str = Field(description="pass | reject | hidden | restore")
    note: str | None = Field(default=None, max_length=256, description="审核备注")


class DmBulkAuditReq(SQLModel):
    """管理端批量人工审核 / 上下架。"""

    msgkeys: list[str] = Field(description="待处理私信 msgkey 列表（字符串）")
    op: str = Field(description="pass | reject | hidden | restore")
    note: str | None = Field(
        default=None, max_length=256, description="统一审核备注（所有条共用，可选）"
    )
    notes: dict[str, str] | None = Field(
        default=None,
        description="逐条审核备注：{ msgkey(字符串): 原因 }，优先级高于 note；留空则该条用 note",
    )


class DmBulkAuditResp(SQLModel):
    """批量审核结果汇总。"""

    total: int = Field(default=0, description="请求条数")
    success: int = Field(default=0, description="成功条数")
    failed: list[str] = Field(default_factory=list, description="失败的 msgkey 列表")


class DmAuditListResp(SQLModel):
    items: list[DmAuditItem] = Field(default_factory=list)
    total: int = 0
    page_num: int = 1
    page_size: int = 20
    states: list[ResourceAuditStatusEnum] = Field(
        default_factory=list, description="本次实际生效的状态过滤（非 root 恒为待审核）"
    )
    can_view_all_states: bool = Field(
        default=False, description="当前管理员是否可查看全部状态（仅 root 为 True）"
    )


class DmSessionContextResp(SQLModel, AutoStrMixin):
    """私信会话上下文（管理端「内容来源」点击后查看前后消息）。"""

    session_key: str
    sender_mid: int = Field(default=0, description="定位消息的发送者mid")
    talker_mid: int = Field(default=0, description="定位消息的对话方mid")
    source: AuditSourceInfo | None = None
    items: list[DmAuditItem] = Field(
        default_factory=list, description="该会话的消息（按 msgkey 倒序）"
    )
    total: int = 0


class DmStatsResp(SQLModel):
    """私信全局统计（管理端）。"""

    total_dm: int = 0
    today_new: int = 0
    auditing: int = 0
    rejected: int = 0
    hidden: int = 0


__all__ = [
    "DmAckReq",
    "DmAuditItem",
    "DmAuditListResp",
    "DmAuditReq",
    "DmDeleteReq",
    "DmMessageItem",
    "DmMessageListResp",
    "DmOperationResp",
    "DmRecallReq",
    "DmSendReq",
    "DmSendResp",
    "DmSessionContextResp",
    "DmSessionDeleteReq",
    "DmSessionItem",
    "DmSessionListResp",
    "DmStatsResp",
    "DmTopReq",
    "DmTopResp",
]
