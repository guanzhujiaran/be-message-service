"""私信模块的请求 / 响应模型。"""

from datetime import datetime

from sqlmodel import Field, SQLModel

from app.models.enums import (
    DmAuditStateEnum,
    DmMsgStatusEnum,
    DmMsgTypeEnum,
    DmRelationEnum,
)
from app.models.schemas.audit import AuditSourceInfo
from app.models.schemas.comment import CommentUserBrief


class DmSendReq(SQLModel):
    """发送一条私信。"""

    receiver_mid: int = Field(description="接收者mid")
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


class DmSessionItem(SQLModel):
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
    is_top: bool = False
    is_muted: bool = False
    updated_at: datetime | None = None


class DmSessionListResp(SQLModel):
    items: list[DmSessionItem] = Field(default_factory=list)
    total: int = 0
    unread_total: int = Field(default=0, description="全部会话未读数之和")
    stranger_unread: int = Field(default=0, description="陌生人会话未读数之和")


class DmMessageItem(SQLModel):
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
    audit_state: DmAuditStateEnum = DmAuditStateEnum.NORMAL


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

    talker_mid: int = Field(description="对话方mid")
    ack_msgkey: str | None = Field(
        default=None, description="已读到的最大 msgkey，为空表示全部已读"
    )


class DmSessionDeleteReq(SQLModel):
    talker_mid: int = Field(description="要删除的会话对方mid")


class DmAuditItem(SQLModel):
    """私信审核队列中的一条消息。"""

    msgkey: str = Field(description="消息全局唯一键（字符串）")
    sender_mid: int = Field(description="发送者mid")
    talker_mid: int = Field(description="对话方mid")
    session_key: str = Field(description="会话键：小mid_大mid")
    message: str = Field(default="", description="正文摘要（内容分片未就绪时兜底）")
    msg_type: DmMsgTypeEnum = DmMsgTypeEnum.TEXT
    audit_state: DmAuditStateEnum = DmAuditStateEnum.NORMAL
    msg_ts: int = Field(default=0, description="消息毫秒时间戳")
    content_ready: bool = Field(default=False, description="内容是否已异步落库到分片")
    created_at: datetime | None = None
    source: AuditSourceInfo | None = Field(
        default=None, description="内容来源，管理端可点击直达该会话上下文"
    )
    sender: CommentUserBrief | None = Field(
        default=None, description="发送者信息，装配时直连 pptr 只读取回"
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
    states: list[DmAuditStateEnum] = Field(
        default_factory=list, description="本次实际生效的状态过滤（非 root 恒为待审核）"
    )
    can_view_all_states: bool = Field(
        default=False, description="当前管理员是否可查看全部状态（仅 root 为 True）"
    )


class DmSessionContextResp(SQLModel):
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
    "DmSendReq",
    "DmSendResp",
    "DmSessionItem",
    "DmSessionListResp",
    "DmMessageItem",
    "DmMessageListResp",
    "DmDeleteReq",
    "DmRecallReq",
    "DmOperationResp",
    "DmAckReq",
    "DmSessionDeleteReq",
    "DmAuditItem",
    "DmAuditReq",
    "DmAuditListResp",
    "DmSessionContextResp",
    "DmStatsResp",
]
