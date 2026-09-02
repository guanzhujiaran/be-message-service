"""通用资源 ORM 模型（be-message MySQL 主库 `BiliMessageDB`）。

任意业务资源（dynamic / lottery / rpa_* / comment 等）共用的明细与流水表：
点赞、点踩、举报、审核记录。Feed 元数据见 `resource_feed_tbl.TResourceFeed`，
收藏明细见 `favorite_tbl.TResourceFavorite`。

约束（对齐 `app.models.db` 既有规范）：
- 表名 `"T"` 前缀 PascalCase；列名 camelCase；
- 资源定位一律 `(bizType, bizId)`，继承 `ResourceBase` 或 `ReportBase`；
- `mid` 仅存 BIGINT，不建跨库外键；
- 索引 / 唯一约束在 `__table_args__` 显式声明。
"""

from sqlalchemy import UniqueConstraint, text
from sqlmodel import Field, Index, PrimaryKeyConstraint
from bili_common.models.report import ReportBase

from app.models.db.base_tbl import ResourceBase, TimestampMixin
from sqlalchemy import Enum as SAEnum
from app.models.enums import (
    MomentAuditLogActionEnum,
    MomentAuditLogOperatorRoleEnum,
    MomentAuditStatusEnum,
)


class TResourceLike(ResourceBase, TimestampMixin, table=True):
    """通用资源点赞明细表（2.55.0 改名 `TMomentLike → TResourceLike`）。

    **继承 `ResourceBase`**（`pk+bizType+bizId+mid+created_at/updated_at`），
    唯一约束 `(bizType, bizId, mid)` 一人一赞；动态资源（bizType=DYNAMIC）时
    `bizId` 与 `dynId` 同义（不再冗余 `dynId` 列，2.55.0 删除以彻底去掉对
    `TMoment` 的 FK 依赖，使任何 bizType 资源都能走同一套明细 + 清理路径）。

    类名/表名从 `TMomentLike` 改为 `TResourceLike`（与 `TResourceDislike` /
    `TResourceAuditLog` / `TResourceReport` / `TResourceFeed` 同前缀；语义上
    Like 适用于任意通用资源）。代码层类名统一，DB 层由你手动更新（计划书
    §3.1.1 已附 DDL）。
    """

    __tablename__ = "TResourceLike"
    __table_args__ = (
        PrimaryKeyConstraint("pk", name="TResourceLike_pkey"),
        UniqueConstraint("bizType", "bizId", "mid", name="TResourceLike_bizType_bizId_mid_key"),
        Index("idx_resource_like_mid_time", "mid", text('created_at DESC')),
        Index("idx_resource_like_biz", "bizType", "bizId"),
        {"extend_existing": True, "comment": "通用资源点赞明细：一人一赞，继承 ResourceBase，唯一约束(bizType,bizId,mid)保证幂等双写；原 TMomentLike"},
    )

    likeType: int = Field(default=1, description="点赞类型：1=普通点赞（预留扩展）")


class TResourceDislike(ResourceBase, TimestampMixin, table=True):
    """通用资源点踩明细表（2.55.0 改名 `TMomentDislike → TResourceDislike`）。

    与点赞对称：一人一踩，唯一约束保证幂等双写；`dislikeCount` 在
    `TInteractionStat`（2.36.0 起统一）同事务原子 ±1，EdgeRank 以
    `dislike_ratio` 降权。**继承 `ResourceBase`**（去 `dynId` 列与 FK）。
    """

    __tablename__ = "TResourceDislike"
    __table_args__ = (
        PrimaryKeyConstraint("pk", name="TResourceDislike_pkey"),
        UniqueConstraint("bizType", "bizId", "mid", name="TResourceDislike_bizType_bizId_mid_key"),
        Index("idx_resource_dislike_biz", "bizType", "bizId"),
        Index("idx_resource_dislike_mid_time", "mid", text('created_at DESC')),
        {"extend_existing": True, "comment": "通用资源点踩明细：一人一踩，继承 ResourceBase，唯一约束(bizType,bizId,mid)保证幂等双写；原 TMomentDislike"},
    )


class TResourceReport(ReportBase, table=True):
    """通用资源举报表（2.37.0 改名自 TMomentReport；继承 bili-common `ReportBase` 同构结构）。

    任意资源（dynamic/lottery/rpa_*/comment/user）可举报：``bizType`` 即业务资源类型
    （InteractionBizTypeEnum 值），``bizId`` 为资源 id（dynamic→dynId，comment→rpid，
    lottery/rpa_*→各自资源 id；bizType+bizId 唯一确定被举报资源）。
    **不再指向 TMoment 的 FK**（lottery/rpa_* 的 bizId 不在 TMoment 表）。

    幂等：`ReportBaseService.record_report` 按 (reportMid, bizType, bizId) 去重；
    业务唯一约束 `TResourceReport_reportMid_bizType_bizId_key` 兜底。
    """

    __tablename__ = "TResourceReport"
    __table_args__ = (
        UniqueConstraint(
            "reportMid", "bizType", "bizId",
            name="TResourceReport_reportMid_bizType_bizId_key",
        ),
        Index("idx_tresource_report_biz", "bizType", "bizId"),
        {"extend_existing": True, "comment": "通用资源举报表：任意资源(bizType+bizId)可举报（继承 ReportBase）"},
    )


class TResourceAuditLog(ResourceBase, TimestampMixin, table=True):
    """**通用资源**审核记录表（2026-09-02 起继承 `ResourceBase`，原 `TMomentAuditLog`）。

    任何 `bizType+bizId` 业务资源（动态 / lottery / rpa_* / comment 等）的
    审核流转都可写入本表；`(bizType,bizId)` 唯一确定被审核资源（与
    `TResourceReport` / `TInteractionStat` 等通用资源型表定位一致）。

    与旧 `TMomentAuditLog` 的差异：
    - **去 `dynId` 冗余列与 `TMoment.dynId` 的 FK**——不再依赖 CASCADE 级联，
      cleanup 改为按 `(bizType=DYNAMIC, bizId IN dyn_ids)` 显式删除；
    - **`operatorMid` 字段名统一为 `mid`**（来自 `ResourceBase`），语义不变
      （作者 / 管理员均通过 `mid` 写入）；schema/API 仍以 `operatorMid` 出参
      保持前端契约稳定（见 `app.models.schemas.moment.MomentAuditLogItem`）；
    - **表名 `TMomentAuditLog → TResourceAuditLog`**（与 `TResourceReport` /
      `TResourceFeed` 同前缀，体现"通用资源"语义）。
    """

    __tablename__ = "TResourceAuditLog"
    __table_args__ = (
        PrimaryKeyConstraint("pk", name="TResourceAuditLog_pkey"),
        Index("idx_audit_log_biz_created", "bizType", "bizId", text('created_at DESC')),
        Index("idx_audit_log_mid_created", "mid", text('created_at DESC')),
        Index("idx_audit_log_action_created", "actionType", text('created_at DESC')),
        {"extend_existing": True, "comment": "通用资源审核流水：bizType+bizId 定位任意资源，继承 ResourceBase"},
    )

    operatorRole: MomentAuditLogOperatorRoleEnum = Field(
        default=None, nullable=False, sa_type=SAEnum(MomentAuditLogOperatorRoleEnum), description="author / admin"
    )
    fromStatus: MomentAuditStatusEnum | None = Field(
        default=None, sa_type=SAEnum(MomentAuditStatusEnum), description="流转前 auditStatus"
    )
    toStatus: MomentAuditStatusEnum = Field(
        default=None, nullable=False, sa_type=SAEnum(MomentAuditStatusEnum), description="流转后 auditStatus"
    )
    actionType: MomentAuditLogActionEnum = Field(
        default=None, nullable=False, sa_type=SAEnum(MomentAuditLogActionEnum), description="create/edit/approve/reject/resubmit/delete"
    )
    rejectReason: str | None = Field(default=None, max_length=500, description="驳回原因（仅 actionType=reject 有值）")
    remark: str | None = Field(default=None, max_length=500, description="其他备注")
    clientIp: str | None = Field(default=None, max_length=64, description="操作者 IP")
    userAgent: str | None = Field(default=None, max_length=512, description="操作者 UA")


__all__ = [
    "TResourceAuditLog",
    "TResourceLike",
    "TResourceDislike",
    "TResourceReport",
]
