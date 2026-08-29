"""动态卡片模块 ORM 模型（be-message MySQL 主库 `BiliMessageDB`）。

本文件定义动态卡片功能所需的全部数据表，由 Alembic `alembic/` 分支
（be-message 主库）统一纳管。

约束（对齐 `app.models.db` 既有规范）：
- 表名沿用计划书冻结的 `"T"` 前缀 PascalCase（TMoment / TMomentLike …）；
- 列名（name）使用 camelCase，Python 属性名与数据库列名完全一致；
- 时间戳统一用 `TimestampMixin`（datetime + default_factory + onupdate）；
- JSON 正文用 `sa.JSON()`；枚举列直接用 `sqlalchemy.Enum(...)`（如 `sa_type=Enum(SomeEnum)`），落库为 MySQL 原生 ENUM 存成员名；
- `mid` 系用户字段仅存 BIGINT，**不建跨库外键**（用户主数据在 pptr Postgres，
  MySQL 主库惯例不引用，渲染时由 PptrUser 只读回查）；
- 自引用（TMoment.repostSrcDynId → TMoment.dynId）用 `SET NULL` 软引用；
- 索引在 `__table_args__` 中显式声明（含命名），DESC 排序用 `text('col DESC')`（MySQL 标识符直接书写，勿加双引号，否则被当作字符串字面量）。
"""

from datetime import datetime

from sqlalchemy import BIGINT, JSON, Text, UniqueConstraint, text
from sqlmodel import Field, ForeignKeyConstraint, Index, PrimaryKeyConstraint
from bili_common.models.report import ReportBase

from app.models.db.base_tbl import TimestampMixin
from sqlalchemy import Enum as SAEnum
from app.models.enums import (
    InteractionBizTypeEnum,
    MomentAuditLogActionEnum,
    MomentAuditLogOperatorRoleEnum,
    MomentAuditStatusEnum,
    MomentFoldTypeEnum,
    MomentReportAuditStatusEnum,
    MomentReportReasonEnum,
    MomentTopicAuditStatusEnum,
    MomentTypeEnum,
    MomentVisibleScopeEnum,
)


class TMoment(TimestampMixin, table=True):
    """动态主表。

    每条动态由多个模块组成，前端按模块顺序渲染。MVP 仅支持 WORD / FORWARD 两类。
    """

    __tablename__ = "TMoment"
    __table_args__ = (
        ForeignKeyConstraint(
            ["repostSrcDynId"], ["TMoment.dynId"], ondelete="SET NULL", name="TMoment_repostSrcDynId_fkey"
        ),
        PrimaryKeyConstraint("dynId", name="TMoment_pkey"),
        Index("idx_dynamic_mid_pubtime", "mid", text('pubTime DESC')),
        Index("idx_dynamic_mid_created", "mid", text('created_at DESC')),
        Index("idx_dynamic_auditing_created", "auditStatus", text('created_at DESC')),
        Index("idx_dynamic_topic_pubtime", "topicId", text('pubTime DESC')),
        Index("idx_dynamic_pubtime_visible", text('pubTime DESC'), "visibleScope"),
        Index("idx_dynamic_repost_src", "repostSrcDynId", "auditStatus"),
        Index("idx_dynamic_biz", "bizType", "bizRid"),
        {"extend_existing": True, "comment": "动态主表：WORD 文字 / FORWARD 转发，MVP 不含图片上传（正文外链图）"},
    )

    # mid 仅存 BIGINT，不建跨库 FK（用户主数据在 pptr Postgres）
    dynId: int = Field(default=None, primary_key=True, sa_type=BIGINT, sa_column_kwargs={"autoincrement": True})
    mid: int = Field(default=None, nullable=False, sa_type=BIGINT, description="发布者 UID")
    dynType: MomentTypeEnum = Field(
        sa_type=SAEnum(MomentTypeEnum), nullable=False, description="动态类型：1=FORWARD, 6=WORD"
    )
    bizRid: int | None = Field(default=None, sa_type=BIGINT, description="业务资源 ID（AV/PGC/LIVE 后续迭代用）")
    bizType: InteractionBizTypeEnum | None = Field(
        default=None,
        sa_type=SAEnum(InteractionBizTypeEnum),
        description="业务资源类型（IntEnum 落库 INT：1=dynamic,2=lottery,...；附加卡/转发引用时写入）",
    )
    contentText: str | None = Field(default=None, sa_type=Text, description="纯文本正文（去标签提取，便于全文搜索）")
    contentJson: dict = Field(default=None, nullable=False, sa_type=JSON, description="结构化正文富文本节点（支持外链图片 URL 节点）")
    repostSrcDynId: int | None = Field(default=None, sa_type=BIGINT, description="转发源动态 ID，自引用；FORWARD 必填")
    repostDepth: int = Field(default=0, description="转发嵌套深度，超过 N 层截断")
    topicId: int | None = Field(default=None, sa_type=BIGINT, description="关联话题 ID")
    lbsPoi: str | None = Field(default=None, max_length=255, description="LBS 位置 POI")
    lbsLat: float | None = Field(default=None, description="纬度")
    lbsLng: float | None = Field(default=None, description="经度")
    ipLocation: str | None = Field(
        default=None, max_length=64, description="IP 属地（国家/省/城市，如「浙江 杭州」；由服务端 GeoIP 解析）"
    )
    ipIsp: str | None = Field(
        default=None, max_length=128, description="IP 运营商 ISP（如「China Education and Research Network」；ASN 库解析）"
    )
    visibleScope: MomentVisibleScopeEnum = Field(
        default=MomentVisibleScopeEnum.PUBLIC, sa_type=SAEnum(MomentVisibleScopeEnum), description="可见范围：0=公开,1=仅关注,2=仅自己,3=充电专享"
    )
    closeComment: int = Field(default=0, description="是否关闭评论：0=否,1=是")
    upChooseComment: int = Field(default=0, description="UP 精选评论开关")
    foldType: MomentFoldTypeEnum = Field(
        default=MomentFoldTypeEnum.NONE, sa_type=SAEnum(MomentFoldTypeEnum), description="折叠类型：0=无,1=用户折叠,2=超频折叠"
    )
    auditStatus: MomentAuditStatusEnum = Field(
        default=MomentAuditStatusEnum.AUDITING,
        sa_type=SAEnum(MomentAuditStatusEnum),
        description="审核状态：auditing/normal/rejected/hidden，发布默认审核中",
    )
    auditRejectReason: str | None = Field(default=None, max_length=500, description="最近一次驳回原因（rejected 状态卡片展示）")
    isTop: int = Field(default=0, description="是否空间置顶：0=否,1=是（仅 normal 可设）")
    topTime: datetime | None = Field(default=None, description="置顶时间")
    timerPubTime: datetime | None = Field(default=None, description="定时发布时间（NULL=立即进入审核队列）")
    pubTime: datetime | None = Field(default=None, description="实际对外发布时间（审核通过时写入 now()），Feed 排序依据")
    deletedAt: datetime | None = Field(default=None, description="软删时间（不为 NULL 时所有对外接口视为不存在）")


class TMomentLike(TimestampMixin, table=True):
    """点赞明细表（2.17.0 泛化：支持任意业务资源 bizType+bizId，幂等双写的关键）。

    唯一约束 `(bizType, bizId, mid)` 一人一赞；动态资源（bizType='dynamic'）
    时 `bizId` 与 `dynId` 冗余相同。`FK(dynId)` 仅在 bizType='dynamic' 时对动态生效，
    非动态资源（lottery/rpa_*）dynId 为 NULL 不受 FK 约束。
    """

    __tablename__ = "TMomentLike"
    __table_args__ = (
        ForeignKeyConstraint(["dynId"], ["TMoment.dynId"], ondelete="CASCADE", name="TMomentLike_dynId_fkey"),
        PrimaryKeyConstraint("pk", name="TMomentLike_pkey"),
        UniqueConstraint("bizType", "bizId", "mid", name="TMomentLike_bizType_bizId_mid_key"),
        Index("idx_like_mid_time", "mid", text('created_at DESC')),
        Index("idx_like_biz", "bizType", "bizId"),
        {"extend_existing": True, "comment": "点赞明细：一人一赞，唯一约束(bizType,bizId,mid)保证幂等双写"},
    )

    pk: int = Field(default=None, primary_key=True, sa_type=BIGINT, sa_column_kwargs={"autoincrement": True})
    bizType: InteractionBizTypeEnum = Field(
        default=InteractionBizTypeEnum.DYNAMIC,
        sa_type=SAEnum(InteractionBizTypeEnum),
        nullable=False,
        description="资源类型（IntEnum 落库 INT）：1=dynamic,2=lottery,3=rpa_action,4=rpa_workflow,5=rpa_browser",
    )
    bizId: int = Field(default=None, nullable=False, sa_type=BIGINT, description="被点赞的资源id（动态时=dynId）")
    # 注意：dynId 需要普通索引 idx_like_dyn 以支撑 FK TMomentLike_dynId_fkey
    dynId: int | None = Field(default=None, sa_type=BIGINT, nullable=True, index=True, description="冗余兼容列：bizType=dynamic 时与 bizId 相同，非动态为 NULL")
    # mid 仅存 BIGINT，不建跨库 FK
    mid: int = Field(default=None, nullable=False, sa_type=BIGINT, description="点赞者 UID")
    likeType: int = Field(default=1, description="点赞类型：1=普通点赞（预留扩展）")


class TMomentDislike(TimestampMixin, table=True):
    """点踩明细表（2.35.0）：幂等，uq(bizType,bizId,mid)。

    与点赞对称：一人一踩，唯一约束保证幂等双写；`dislikeCount` 在
    `TInteractionStat`（2.36.0 起统一）同事务原子 ±1，EdgeRank 以
    `dislike_ratio` 降权。
    """

    __tablename__ = "TMomentDislike"
    __table_args__ = (
        ForeignKeyConstraint(["dynId"], ["TMoment.dynId"], ondelete="CASCADE", name="TMomentDislike_dynId_fkey"),
        PrimaryKeyConstraint("pk", name="TMomentDislike_pkey"),
        UniqueConstraint("bizType", "bizId", "mid", name="TMomentDislike_bizType_bizId_mid_key"),
        Index("idx_dislike_biz", "bizType", "bizId"),
        Index("idx_dislike_mid_time", "mid", text('created_at DESC')),
        {"extend_existing": True, "comment": "点踩明细：一人一踩，唯一约束(bizType,bizId,mid)保证幂等双写"},
    )

    pk: int = Field(default=None, primary_key=True, sa_type=BIGINT, sa_column_kwargs={"autoincrement": True})
    bizType: InteractionBizTypeEnum = Field(
        default=InteractionBizTypeEnum.DYNAMIC,
        sa_type=SAEnum(InteractionBizTypeEnum),
        nullable=False,
        description="资源类型（IntEnum 落库 INT）",
    )
    bizId: int = Field(default=None, nullable=False, sa_type=BIGINT, description="被点踩的资源id（动态时=dynId）")
    dynId: int | None = Field(default=None, sa_type=BIGINT, nullable=True, index=True, description="冗余兼容列：bizType=dynamic 时与 bizId 相同，非动态为 NULL")
    # mid 仅存 BIGINT，不建跨库 FK
    mid: int = Field(default=None, nullable=False, sa_type=BIGINT, description="点踩者 UID")


class MomentAuthorQuality(TimestampMixin, table=True):
    """作者质量聚合表（2.35.0）：EdgeRank 作者维度信号，定时任务计算。

    存作者维度聚合（不依赖 pptr 粉丝数据，以本库动态互动自洽）：
    - ``avgEngagement``：作者全部 normal 动态的平均互动率 `(like+comment+repost)/max(view,1)`；
    - ``recentPublishCount``：近 7 天发布量（刷屏降权依据）；
    - ``violationCount``：被驳回/下架次数（违规降权依据）。
    """

    __tablename__ = "moment_author_quality"
    __table_args__ = (
        PrimaryKeyConstraint("mid", name="moment_author_quality_pkey"),
        {"extend_existing": True, "comment": "作者质量聚合：平均互动率/近7天发布量/违规数"},
    )

    mid: int = Field(default=None, primary_key=True, sa_type=BIGINT, sa_column_kwargs={"autoincrement": False})
    avgEngagement: float = Field(default=0.0, description="作者平均互动率 (like+comment+repost)/max(view,1)")
    recentPublishCount: int = Field(default=0, description="近 7 天发布量")
    violationCount: int = Field(default=0, description="违规数（被驳回/下架次数）")
    # 2.37.0 作者粉丝/等级维度（定时任务低频聚合，非热路径）
    fansCount: int = Field(default=0, sa_type=BIGINT, description="≈ 被关注数（msg_user_follow 按 target_mid COUNT）")
    currentLevel: int = Field(default=0, sa_type=BIGINT, description="作者等级（pptr PptrUserLevel.current_level 回查）")


class TMomentTopic(TimestampMixin, table=True):
    """动态话题表（话题广场 / 话题 Feed / 用户创建话题审核）。"""

    __tablename__ = "TMomentTopic"
    __table_args__ = (
        UniqueConstraint("topicName", name="TMomentTopic_topicName_key"),
        PrimaryKeyConstraint("topicId", name="TMomentTopic_pkey"),
        Index("idx_topic_audit_created", "auditStatus", text('created_at DESC')),
        Index("idx_topic_creator_created", "creatorMid", text('created_at DESC')),
        {"extend_existing": True, "comment": "动态话题表：话题广场列表、话题下动态计数、用户创建话题审核等"},
    )

    # 对外发布 ID 一律雪花 ID（见规则 snowflake-id.mdc）：topicId 由应用层 generate_topic_id() 生成，非自增
    topicId: int = Field(default=None, primary_key=True, sa_type=BIGINT, sa_column_kwargs={"autoincrement": False})
    topicName: str = Field(default=None, nullable=False, max_length=100, description="话题名称（UNIQUE）")
    topicCover: str | None = Field(default=None, max_length=1024, description="话题封面图")
    topicDesc: str | None = Field(default=None, sa_type=Text, description="话题描述")
    jumpUrl: str | None = Field(default=None, max_length=1024, description="话题跳转 H5")
    dynCount: int = Field(default=0, sa_type=BIGINT, description="话题下动态数（仅 normal 且未软删）")
    viewCount: int = Field(default=0, sa_type=BIGINT, description="话题浏览量")
    isHot: int = Field(default=0, description="是否热门话题")
    sortWeight: int = Field(default=0, description="广场排序权重")
    # 话题创建与审核（2.19.0 起；seed 灌入的真实话题置 0 表示系统/预置）
    creatorMid: int = Field(default=0, nullable=False, sa_type=BIGINT, description="创建者 UID（0=系统/seed 预置）")
    auditStatus: MomentTopicAuditStatusEnum = Field(
        default=MomentTopicAuditStatusEnum.AUDITING,
        sa_type=SAEnum(MomentTopicAuditStatusEnum),
        description="审核状态：auditing/normal/rejected（用户创建默认 auditing，不公开展示）",
    )
    auditRejectReason: str | None = Field(default=None, max_length=500, description="最近一次驳回原因")
    pubTime: datetime | None = Field(default=None, description="实际对外发布时间（审核通过时写入 now()）")


class TMomentTopicRel(TimestampMixin, table=True):
    """动态-话题多对多关系表（2.22.0）。

    一条动态可关联多个话题（上限 `_MAX_TOPIC_COUNT`=5，去重）。
    只存 `topicId`，**不冗余存话题名称快照**（名称只读 `TMomentTopic` 回填，
    话题改名全局生效）；`TMoment.topicId` 保留为主话题（= topics[0]），
    兼容存量单话题数据与话题 Feed 主查询（`idx_dynamic_topic_pubtime`）。
    """

    __tablename__ = "TMomentTopicRel"
    __table_args__ = (
        ForeignKeyConstraint(["dynId"], ["TMoment.dynId"], ondelete="CASCADE", name="TMomentTopicRel_dynId_fkey"),
        PrimaryKeyConstraint("pk", name="TMomentTopicRel_pkey"),
        UniqueConstraint("dynId", "topicId", name="TMomentTopicRel_dynId_topicId_key"),
        Index("idx_topic_rel_topic", "topicId", "dynId"),
        {"extend_existing": True, "comment": "动态-话题多对多关系：一条动态多话题（上限5），TMoment.topicId=主话题"},
    )

    pk: int = Field(default=None, primary_key=True, sa_type=BIGINT, sa_column_kwargs={"autoincrement": True})
    dynId: int = Field(default=None, nullable=False, sa_type=BIGINT, description="动态 ID")
    topicId: int = Field(default=None, nullable=False, sa_type=BIGINT, description="话题 ID（仅存 ID，名称只读 TMomentTopic）")


class TResourceReport(ReportBase, table=True):
    """通用资源举报表（2.37.0 改名自 TMomentReport；继承 bili-common `ReportBase` 同构结构）。

    任意资源（dynamic/lottery/rpa_*）可举报：``bizType`` 标识举报来源（dynamic），
    ``resourceType``（InteractionBizTypeEnum 值）标识被举报资源类型；``bizId`` 为资源 id
    （dynamic→dynId）。**不再指向 TMoment 的 FK**（lottery/rpa_* 的 bizId 不在 TMoment 表）。

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


class TMomentAuditLog(TimestampMixin, table=True):
    """动态审核记录表（记录每一次审核流转用于审计 / 后台流水）。"""

    __tablename__ = "TMomentAuditLog"
    __table_args__ = (
        ForeignKeyConstraint(["dynId"], ["TMoment.dynId"], ondelete="CASCADE", name="TMomentAuditLog_dynId_fkey"),
        PrimaryKeyConstraint("pk", name="TMomentAuditLog_pkey"),
        Index("idx_audit_log_dynid_created", "dynId", text('created_at DESC')),
        Index("idx_audit_log_admin_created", "operatorMid", text('created_at DESC')),
        Index("idx_audit_log_action_created", "actionType", text('created_at DESC')),
        {"extend_existing": True, "comment": "动态审核记录：发布/编辑/通过/驳回等流转流水"},
    )

    pk: int = Field(default=None, primary_key=True, sa_type=BIGINT, sa_column_kwargs={"autoincrement": True})
    dynId: int = Field(default=None, nullable=False, sa_type=BIGINT, description="被审核的动态")
    operatorMid: int = Field(default=None, nullable=False, sa_type=BIGINT, description="操作人 MID（作者=发布/编辑；管理员=通过/驳回）")
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
    "TMoment",
    "TMomentAuditLog",
    "TMomentLike",
    "TResourceReport",
    "TMomentTopic",
    "TMomentTopicRel",
]
