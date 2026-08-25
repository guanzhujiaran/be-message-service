"""通用资源 Feed 元数据表 ORM（2.36.0）。

`TResourceFeed` 承载**任意资源（bizType+bizId）统一入 Feed** 所需的排序元数据：
作者 mid / 发布时间 pubTime / 审核状态 auditStatus / 标签 tags / 软删 deletedAt。

- 动态（bizType=dynamic，bizId=dynId）：发布时写行（auditing），审核通过写 pubTime 且
  auditStatus=normal；feed 引擎经本表取候选，内容渲染回 `TMoment`；
- 其他资源（lottery / rpa_*）：各自业务在可推荐时写入本表（auditStatus=normal），
  feed 引擎统一读取，详情经 RPC 实时获取。

计数不冗余在本表：统一读 `TInteractionStat`（2.36.0 起动态并入）+
`CommentSubject`（评论系统实时计数）。
"""

from datetime import datetime

from sqlalchemy import BIGINT, JSON, Index, PrimaryKeyConstraint, text
from sqlmodel import Field

from app.models.db.base import TimestampMixin
from sqlalchemy import Enum as SAEnum
from app.models.enums import InteractionBizTypeEnum


class TResourceFeed(TimestampMixin, table=True):
    """通用资源 Feed 元数据：每种可入 Feed 的 (bizType, bizId) 一行。"""

    __tablename__ = "TResourceFeed"
    __table_args__ = (
        PrimaryKeyConstraint("bizType", "bizId", name="TResourceFeed_pkey"),
        Index("idx_resfeed_status_pubtime", "auditStatus", text("pubTime DESC")),
        Index("idx_resfeed_mid_pubtime", "mid", text("pubTime DESC")),
        {"extend_existing": True, "comment": "通用资源 Feed 元数据：bizType+bizId 统一入 Feed 的排序元数据"},
    )

    bizType: InteractionBizTypeEnum = Field(
        primary_key=True,
        sa_type=SAEnum(InteractionBizTypeEnum),
        sa_column_kwargs={"autoincrement": False},
        description="资源类型（IntEnum 落库 INT）：1=dynamic,2=lottery,...",
    )
    bizId: int = Field(
        primary_key=True,
        sa_type=BIGINT,
        sa_column_kwargs={"autoincrement": False},
        description="资源 id（动态=dynId，均为雪花 id）",
    )
    mid: int | None = Field(
        default=None,
        nullable=True,
        sa_type=BIGINT,
        description="资源作者 UID（可能无作者，如 lottery/rpa_*；2.37.0 起可空）",
    )
    pubTime: datetime | None = Field(default=None, description="实际对外发布时间（审核通过写入），Feed 排序依据")
    auditStatus: str = Field(
        default=None,
        max_length=32,
        description="资源审核状态字符串（各资源自定；'normal' 表示可公开入 Feed）",
    )
    tags: list = Field(
        default_factory=list,
        sa_type=JSON,
        description="标签 id 列表：dynamic=话题 id（TMomentTopicRel 冗余，供个性化/话题流）；其他资源=分类/空",
    )
    deletedAt: datetime | None = Field(default=None, description="软删时间（不为 NULL 时不入 Feed）")


__all__ = ["TResourceFeed"]
