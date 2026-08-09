"""事件提醒（点赞 / 回复 / @提及）相关表模型。

事件提醒是典型的「写扩散」场景：一次行为只产生一条给接收者的记录，
因此直接按接收者 mid 落一行即可。

聚合展示的关键在索引设计：
`(mid, event_type, source_type, source_id)` 联合索引让
「同一篇稿件下的 N 个点赞聚合成一条」可以走索引 GROUP BY，
而不需要额外维护一张聚合表（小设备上省一张表、省一次写）。

去重靠 `dedup_key` 唯一索引：同一个人对同一实体的同类行为只记一次，
重复上报（MQ 重投、前端重试）会被数据库直接拦掉，实现幂等消费。
"""

from datetime import datetime

from sqlalchemy import BIGINT, Text
from sqlmodel import Column, Field, Index, SQLModel, UniqueConstraint

from app.models.db.base import TimestampMixin, str_enum_type
from app.models.enums import EventTypeEnum, SourceTypeEnum


class EventMessage(TimestampMixin, table=True):
    """一条事件提醒（接收者视角）。"""

    __tablename__ = "msg_event"
    __table_args__ = (
        # 聚合查询主索引：按用户 + 类型 + 来源分组
        Index("idx_event_group", "mid", "event_type", "source_type", "source_id"),
        # 时间线索引：按用户 + 类型倒序翻页
        Index("idx_event_timeline", "mid", "event_type", "id"),
        UniqueConstraint("dedup_key", name="uq_event_dedup"),
        {"extend_existing": True},
    )

    id: int | None = Field(default=None, primary_key=True)

    mid: int = Field(sa_type=BIGINT, index=True, description="接收者mid")
    event_type: EventTypeEnum = Field(
        sa_type=str_enum_type(EventTypeEnum), description="事件类型"
    )

    # ---- 聚合分组键 ----
    source_type: SourceTypeEnum = Field(
        default=SourceTypeEnum.OTHER,
        sa_type=str_enum_type(SourceTypeEnum),
        description="来源实体类型",
    )
    source_id: str = Field(max_length=64, description="来源实体id")
    source_title: str | None = Field(
        default=None, max_length=256, description="来源实体标题（聚合卡片展示用）"
    )
    source_cover: str | None = Field(
        default=None, max_length=512, description="来源实体封面"
    )

    # ---- 触发者 ----
    actor_mid: int = Field(sa_type=BIGINT, index=True, description="触发行为的用户mid")
    actor_name: str | None = Field(default=None, max_length=64, description="触发者昵称")
    actor_avatar: str | None = Field(default=None, max_length=512, description="触发者头像")

    content: str | None = Field(
        default=None, sa_column=Column(Text), description="事件内容（回复正文 / @上下文）"
    )
    jump_url: str | None = Field(default=None, max_length=512, description="跳转链接")

    is_read: bool = Field(default=False, index=True, description="是否已读")
    read_at: datetime | None = Field(default=None, description="已读时间")
    is_deleted: bool = Field(default=False, description="是否已删除")

    # ---- 推送相关（与消息推送子系统对齐；本地库 msg_event 已存在这两列）----
    push_strategy: str | None = Field(
        default=None, max_length=16, description="推送策略"
    )
    pushed: bool = Field(default=False, index=True, description="是否已推送")

    dedup_key: str = Field(
        max_length=128,
        description="幂等键：event_type:actor_mid:source_type:source_id:biz_id 的摘要",
    )


class EventReadCursor(TimestampMixin, table=True):
    """按事件类型维护的已读游标（一键已读用）。"""

    __tablename__ = "msg_event_cursor"
    __table_args__ = (
        UniqueConstraint("mid", "event_type", name="uq_event_cursor_mid_type"),
        {"extend_existing": True},
    )

    id: int | None = Field(default=None, primary_key=True)
    mid: int = Field(sa_type=BIGINT, index=True, description="用户mid")
    event_type: EventTypeEnum = Field(
        sa_type=str_enum_type(EventTypeEnum), description="事件类型"
    )
    last_read_id: int = Field(default=0, description="已读到的最大事件id")
    last_read_at: datetime | None = Field(default=None, description="上次一键已读时间")


__all__ = ["EventMessage", "EventReadCursor", "SQLModel"]
