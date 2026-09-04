"""统一举报系统请求 / 响应模型（2.14.0）。

统一举报接口 `POST /api/v1/report` 以 `biz_type` + `biz_id` 区分来源
（dynamic / comment / user），原因枚举对齐 B 站（`ReportReasonEnum`），
并支持可选图片附件 `pics`（站外 http(s) URL 列表，最多 3 张）。
"""

from sqlmodel import Field, SQLModel

from bili_common.models import InteractionBizTypeEnum
from app.models.str_int import StrInt
from app.models.schemas.interaction import InteractionResource


from app.models.schemas.base import AutoStrMixin
class ReportCreateReq(SQLModel, AutoStrMixin):
    """统一举报请求（评论 / 动态 / 用户空间 / RPA 资源）。"""

    bizType: InteractionBizTypeEnum = Field(description="举报来源类型（InteractionBizTypeEnum 值，即业务资源类型：dynamic/lottery/rpa_*/comment/user）")
    bizId: StrInt = Field(description="被举报对象 id：dynamic→dynId，comment→rpid，user→mid，lottery/rpa_*→各自资源 id（雪花 ID，StrInt 兼容前端 str 传参）")
    reasonType: int = Field(description="统一举报原因（ReportReasonEnum 值）")
    reasonDesc: str | None = Field(default=None, max_length=500, description="补充描述（选填）")
    pics: list[str] | None = Field(default=None, description="证据图片 URL 列表（http(s)，最多 3 张）")


class ReportItem(SQLModel, AutoStrMixin):
    """统一举报记录展示项。"""

    pk: int
    bizType: InteractionBizTypeEnum
    bizId: int
    accusedMid: int
    reportMid: int
    reasonType: int
    reasonDesc: str | None = None
    pics: list[str] | None = None
    auditStatus: str
    auditRemark: str | None = None
    auditAdminMid: int | None = None
    createdAt: str | None = None
    # 2.40.0：被举报数量双口径（跨来源/跨状态，管理端展示）
    reportCount: int = Field(default=0, description="被举报次数（累计，COUNT(*)）")
    reportPeopleCount: int = Field(default=0, description="举报人数（去重，COUNT(DISTINCT reportMid)）")
    # 2.61.0：用户展示信息（举报人 / 被举报人，一次 PptrUser.get_many 批量回查；
    # 弱依赖，回查失败为 null，前端降级「用户{mid}」；见计划书 §5.19）
    reporterName: str | None = Field(default=None, description="举报人昵称")
    reporterFace: str | None = Field(default=None, description="举报人头像")
    accusedName: str | None = Field(default=None, description="被举报人昵称")
    accusedFace: str | None = Field(default=None, description="被举报人头像")
    # 2.61.0：被举报资源快照（按 bizType 经 batch_get_resources 批量回捞，§5.19）
    resource: InteractionResource | None = Field(
        default=None,
        description="被举报资源快照（标题 / 封面 / 跳转目标；exists=false 表示内容已删除或不可见）",
    )


class ReportListResp(SQLModel, AutoStrMixin):
    """统一举报管理端列表响应。"""

    items: list[ReportItem]
    total: int
    page: int
    pageSize: int


class ReportReviewReq(SQLModel, AutoStrMixin):
    """统一举报管理端审核请求。"""

    reportPk: int = Field(description="举报记录主键")
    decision: str = Field(description="处置动作：resolve（属实已处理）/ reject（驳回）")
    resourceAction: str | None = Field(
        default=None,
        description="资源处置动作（2.38.0，resolve 时可选）：hide=下架/隐藏被举报资源（动态→hidden，评论→hidden，用户预留）；None=仅标记不处置",
    )
    remark: str | None = Field(default=None, max_length=500, description="审核备注")


__all__ = [
    "ReportCreateReq",
    "ReportItem",
    "ReportListResp",
    "ReportReviewReq",
]
