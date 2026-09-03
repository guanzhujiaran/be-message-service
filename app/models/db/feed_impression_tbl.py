"""通用 Feed 曝光（已下发）记录表 ORM（2.47.0）。

`TFeedImpression` 记录「某观众在某场景下已被下发过哪些资源」，用于 Feed **曝光去重**
——避免用户反复刷到已经看过的重复内容（计划书 §5.4「尽力去重 + 自动降级」）。

设计要点：

- **观众主体 ``viewerKey``**：登录用户 ``mid:{mid}``，匿名用户 ``anon:{uniq_id}``
  （匿名且未传 ``uniq_id`` 时无法标识，跳过曝光去重，退化为客户端 ``last_showlist``）；
  用单一字符串列而非 ``mid`` 可空列，规避 MySQL 唯一约束对 NULL 不去重的问题。
- **场景隔离 ``feedScene``**：``comprehensive``（综合页）/ ``topic``（话题流）等，
  同一资源在不同 Feed 场景各自计一次曝光（进入话题页看到首页刷过的内容属正常）。
- **唯一约束 ``(viewerKey, bizType, bizId, feedScene)``**：一行 = 一个观众在一个场景下
  对一个资源的累计曝光；``impressionCount`` 累计次数、``lastImpressionAt`` 最后下发时间
  （TTL 窗口判重依据）。
- **写入**：每次下发后批量 upsert（单条 ``INSERT ... ON DUPLICATE KEY UPDATE``），
  非热路径 COUNT 聚合；``settings.edgerank_dedup_enabled=False`` 时完全不读写本表。
"""

from datetime import datetime

from sqlalchemy import BIGINT, Index, PrimaryKeyConstraint, UniqueConstraint, text
from sqlalchemy import Enum as SAEnum
from sqlmodel import Field

from app.models.db.base_tbl import TimestampMixin
from bili_common.models import InteractionBizTypeEnum


class TFeedImpression(TimestampMixin, table=True):
    """通用 Feed 曝光记录：一个观众在一个场景下一个资源一行（累计曝光次数）。"""

    __tablename__ = "TFeedImpression"
    __table_args__ = (
        PrimaryKeyConstraint("pk", name="TFeedImpression_pkey"),
        UniqueConstraint(
            "viewerKey",
            "bizType",
            "bizId",
            "feedScene",
            name="TFeedImpression_viewerKey_bizType_bizId_feedScene_key",
        ),
        # TTL 窗口内「已下发」点查：按观众 + 场景 + 最后下发时间倒序
        Index(
            "idx_feed_impression_viewer_scene_time",
            "viewerKey",
            "feedScene",
            text("lastImpressionAt DESC"),
        ),
        {
            "extend_existing": True,
            "comment": "通用 Feed 曝光记录：观众在某场景已下发资源（曝光去重，防重复刷到）",
        },
    )

    pk: int = Field(
        default=None,
        primary_key=True,
        sa_type=BIGINT,
        sa_column_kwargs={"autoincrement": True},
        description="自增主键",
    )
    viewerKey: str = Field(
        max_length=96,
        nullable=False,
        description="观众标识：登录=mid:{mid}，匿名=anon:{uniq_id}（无 uniq_id 时不做曝光去重）",
    )
    bizType: InteractionBizTypeEnum = Field(
        nullable=False,
        sa_type=SAEnum(InteractionBizTypeEnum),
        description="资源类型（IntEnum 落库）：1=dynamic,2=lottery,...",
    )
    bizId: int = Field(
        nullable=False,
        sa_type=BIGINT,
        description="资源 id（动态=dynId）",
    )
    feedScene: str = Field(
        max_length=32,
        nullable=False,
        description="Feed 场景：comprehensive=综合页 / topic=话题流（跨场景各自计曝光）",
    )
    impressionCount: int = Field(
        default=1,
        description="累计曝光次数（重复下发递增）",
    )
    firstImpressionAt: datetime = Field(
        default=None,
        nullable=False,
        description="首次下发时间",
    )
    lastImpressionAt: datetime = Field(
        default=None,
        nullable=False,
        description="最后下发时间（TTL 窗口判重依据）",
    )


__all__ = ["TFeedImpression"]
