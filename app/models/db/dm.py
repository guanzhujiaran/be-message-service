"""私信（单聊）相关表模型。

单聊采用**写扩散**：一次发送产生两份「用户视角」的数据 ——
发送方一份、接收方一份。好处是读路径极简（只查自己那一份，
无需 JOIN、无需按 session 反查），代价是写放大 2 倍；
对单聊而言放大系数固定为 2，完全可接受。

存储分层：

- `msg_dm_session`（主库）：会话列表，owner_mid 视角，每个会话一行。
- `msg_dm_index`（主库）：消息索引，只存 msgkey / 状态 / 时间等轻量字段。
- 消息**内容**不在主库：按 msgkey 内嵌的时间戳分月度库、按 msgkey 取余分 100 表
  （见 app.core.sharding），且由 MQ **异步**落库，以抬高写性能天花板。

为了让「内容异步化」不影响读的可用性，索引表冗余了 `content_preview`
（正文摘要）与 `content_ready`（落库完成标记）：内容分片尚未就绪时，
读接口直接用摘要兜底返回，用户无感知。
"""

from datetime import datetime

from sqlalchemy import BIGINT
from sqlmodel import Field, Index, SQLModel, UniqueConstraint

from app.models.db.base import TimestampMixin, int_enum_type, str_enum_type
from app.models.enums import (
    DmAuditStateEnum,
    DmMsgStatusEnum,
    DmMsgTypeEnum,
    DmRelationEnum,
    DmSessionTypeEnum,
)


class DmSession(TimestampMixin, table=True):
    """私信会话（写扩散，owner_mid 视角每个会话一行）。"""

    __tablename__ = "msg_dm_session"
    __table_args__ = (
        UniqueConstraint("owner_mid", "talker_mid", name="uq_dm_session_owner_talker"),
        # 会话列表：按归属者 + 最后消息时间倒序
        Index("idx_dm_session_list", "owner_mid", "is_deleted", "last_msg_ts"),
        {"extend_existing": True},
    )

    id: int | None = Field(default=None, primary_key=True)

    owner_mid: int = Field(sa_type=BIGINT, index=True, description="会话归属者mid")
    talker_mid: int = Field(sa_type=BIGINT, index=True, description="对话方mid")
    session_key: str = Field(
        max_length=64, index=True, description="会话键：小mid_大mid，双方共用"
    )
    session_type: DmSessionTypeEnum = Field(
        default=DmSessionTypeEnum.SINGLE,
        sa_type=int_enum_type(DmSessionTypeEnum),
        description="会话类型",
    )

    talker_name: str | None = Field(default=None, max_length=64, description="对方昵称")
    talker_avatar: str | None = Field(default=None, max_length=512, description="对方头像")

    # ---- 最后一条消息快照（会话列表直接展示，避免回查消息表）----
    last_msgkey: int | None = Field(
        default=None, sa_type=BIGINT, description="最后一条消息的msgkey"
    )
    last_content_preview: str | None = Field(
        default=None, max_length=256, description="最后一条消息摘要"
    )
    last_msg_ts: int = Field(
        default=0, sa_type=BIGINT, index=True, description="最后一条消息毫秒时间戳"
    )
    last_sender_uid: int | None = Field(
        default=None, sa_type=BIGINT, description="最后一条消息的发送者"
    )

    unread_count: int = Field(default=0, description="未读数")
    # 已读水位：owner 已读到的最大 msgkey，撤回 / 删除不影响该水位
    ack_msgkey: int | None = Field(
        default=None, sa_type=BIGINT, description="已读水位（最大已读msgkey）"
    )

    relation: DmRelationEnum = Field(
        default=DmRelationEnum.NORMAL,
        sa_type=str_enum_type(DmRelationEnum),
        index=True,
        description="会话关系：normal / stranger",
    )
    is_top: bool = Field(default=False, description="是否置顶")
    is_muted: bool = Field(default=False, description="是否免打扰")
    is_deleted: bool = Field(default=False, index=True, description="是否已删除会话")


class DmMessageIndex(TimestampMixin, table=True):
    """私信消息索引（写扩散，收发双方各一行）。"""

    __tablename__ = "msg_dm_index"
    __table_args__ = (
        UniqueConstraint("owner_mid", "msgkey", name="uq_dm_index_owner_msgkey"),
        # 聊天记录翻页：按归属者 + 对话方 + msgkey 倒序（msgkey 单调递增，等价于按时间）
        Index("idx_dm_index_chat", "owner_mid", "talker_mid", "msgkey"),
        {"extend_existing": True},
    )

    id: int | None = Field(default=None, primary_key=True)

    owner_mid: int = Field(sa_type=BIGINT, index=True, description="该行归属者mid")
    talker_mid: int = Field(sa_type=BIGINT, description="对话方mid")
    session_key: str = Field(max_length=64, index=True, description="会话键：小mid_大mid")

    msgkey: int = Field(
        sa_type=BIGINT, index=True, description="消息全局唯一键（内嵌时间戳，用于分库分表路由）"
    )
    sender_uid: int = Field(sa_type=BIGINT, description="发送者mid")
    msg_type: DmMsgTypeEnum = Field(
        default=DmMsgTypeEnum.TEXT, sa_type=str_enum_type(DmMsgTypeEnum)
    )
    msg_status: DmMsgStatusEnum = Field(
        default=DmMsgStatusEnum.NORMAL,
        sa_type=int_enum_type(DmMsgStatusEnum),
        index=True,
        description="该视角下的消息状态",
    )
    msg_ts: int = Field(sa_type=BIGINT, index=True, description="消息毫秒时间戳")

    # ---- 内容异步化的读侧兜底 ----
    content_preview: str | None = Field(
        default=None, max_length=256, description="正文摘要：内容分片未就绪时兜底返回"
    )
    content_ready: bool = Field(
        default=False, index=True, description="内容是否已异步落库到分片"
    )

    # ---- 管理端审核状态（与评论审核对齐）----
    audit_state: DmAuditStateEnum = Field(
        default=DmAuditStateEnum.NORMAL,
        sa_type=str_enum_type(DmAuditStateEnum),
        index=True,
        description="管理端审核状态：normal/auditing/rejected/hidden",
    )

    recalled_at: datetime | None = Field(default=None, description="撤回时间")


class DmContentDeadLetter(TimestampMixin, table=True):
    """私信内容异步落库失败的死信记录。

    异步化的代价是「可能写失败」。失败的消息落到这张表，
    由定时任务重试补偿，保证内容最终一致，不会因为 MQ 抖动丢正文。
    """

    __tablename__ = "msg_dm_content_dlq"
    __table_args__ = (
        UniqueConstraint("msgkey", name="uq_dm_dlq_msgkey"),
        {"extend_existing": True},
    )

    id: int | None = Field(default=None, primary_key=True)
    msgkey: int = Field(sa_type=BIGINT, index=True, description="消息msgkey")
    session_key: str = Field(max_length=64)
    sender_uid: int = Field(sa_type=BIGINT)
    receiver_uid: int = Field(sa_type=BIGINT)
    msg_type: DmMsgTypeEnum = Field(
        default=DmMsgTypeEnum.TEXT, sa_type=str_enum_type(DmMsgTypeEnum)
    )
    content: str | None = Field(default=None, description="待补写的正文")
    msg_ts: int = Field(sa_type=BIGINT)
    retry_count: int = Field(default=0, index=True, description="已重试次数")
    last_error: str | None = Field(default=None, max_length=512, description="最后一次错误")
    resolved: bool = Field(default=False, index=True, description="是否已补写成功")


__all__ = ["DmSession", "DmMessageIndex", "DmContentDeadLetter", "SQLModel"]
