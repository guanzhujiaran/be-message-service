"""事件提醒（点赞 / 回复 / @提及）相关表模型。

事件提醒是典型的「写扩散」场景：一次行为只产生一条给接收者的记录，
因此直接按接收者 mid 落一行即可。

聚合展示的关键在索引设计：
`(mid, event_type, source_type, source_id)` 联合索引让
「同一篇稿件下的 N 个点赞聚合成一条」可以走索引 GROUP BY，
而不需要额外维护一张聚合表（小设备上省一张表、省一次写）。

去重靠 `dedup_key` 唯一索引：同一个人对同一实体的同类行为只记一次，
重复上报（MQ 重投、前端重试）会被数据库直接拦掉，实现幂等消费。

冗余字段说明：事件表**只存定位所需的 id 与事件自身正文**，
触发者昵称 / 头像（`actor_name` / `actor_avatar`）与来源标题 / 封面 / 跳转
（`source_title` / `source_cover` / `jump_url`）均不落库，
读取时分别按 `actor_mid` 回查用户服务、按 `source_id` + `source_type` 实时回捞原资源，
避免两份数据、省空间、且不易过期。
"""

from datetime import datetime

from sqlalchemy import BIGINT, Text
from sqlmodel import Column, Field, Index, SQLModel, UniqueConstraint

from app.models.db.base_tbl import TimestampMixin
from sqlalchemy import Enum as SAEnum
from bili_common.models import InteractionActionTypeEnum, InteractionBizTypeEnum


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
    event_type: InteractionActionTypeEnum = Field(
        sa_type=SAEnum(InteractionActionTypeEnum), description="事件类型"
    )

    # ---- 聚合分组键 ----
    source_type: InteractionBizTypeEnum = Field(
        sa_type=SAEnum(InteractionBizTypeEnum),
        description="来源实体类型（必填：无对应资源时禁止落库）",
    )
    source_id: str = Field(max_length=64, description="来源实体id")
    biz_id: str | None = Field(
        default=None,
        max_length=64,
        description="业务资源id（如评论rpid / 动态dynId），与 source_type 共同唯一定位原资源，供前端跳转；为空时同人对同实体的同类行为只记一条",
    )

    # ---- 触发者（仅存 mid，昵称 / 头像读取时按 mid 回查用户服务）----
    actor_mid: int = Field(sa_type=BIGINT, index=True, description="触发行为的用户mid")

    content: str | None = Field(
        default=None,
        sa_column=Column(Text),
        description="事件内容（回复正文 / @上下文）",
    )

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
    event_type: InteractionActionTypeEnum = Field(
        sa_type=SAEnum(InteractionActionTypeEnum), description="事件类型"
    )
    last_read_id: int = Field(default=0, description="已读到的最大事件id")
    last_read_at: datetime | None = Field(default=None, description="上次一键已读时间")


__all__ = ["EventMessage", "EventReadCursor", "SQLModel"]
