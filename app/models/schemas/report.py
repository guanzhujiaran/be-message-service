"""统一举报系统请求 / 响应模型（2.14.0）。

统一举报接口 `POST /api/v1/report` 以 `biz_type` + `biz_id` 区分来源
（dynamic / comment / user），原因枚举对齐 B 站（`ReportReasonEnum`），
并支持可选图片附件 `pics`（站外 http(s) URL 列表，最多 3 张）。
"""

from sqlmodel import Field, SQLModel


class ReportCreateReq(SQLModel):
    """统一举报请求（评论 / 动态 / 用户空间 / RPA 资源）。"""

    bizType: str = Field(description="举报来源类型：dynamic/comment/user（ReportBizTypeEnum 值）")
    bizId: int = Field(description="被举报对象 id：dynamic→dynId，comment→rpid，user→mid")
    reasonType: int = Field(description="统一举报原因（ReportReasonEnum 值）")
    reasonDesc: str | None = Field(default=None, max_length=500, description="补充描述（选填）")
    pics: list[str] | None = Field(default=None, description="证据图片 URL 列表（http(s)，最多 3 张）")


class ReportItem(SQLModel):
    """统一举报记录展示项。"""

    pk: int
    bizType: str
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


class ReportListResp(SQLModel):
    """统一举报管理端列表响应。"""

    items: list[ReportItem]
    total: int
    page: int
    pageSize: int


class ReportReviewReq(SQLModel):
    """统一举报管理端审核请求。"""

    reportPk: int = Field(description="举报记录主键")
    decision: str = Field(description="处置动作：resolve（属实已处理）/ reject（驳回）")
    remark: str | None = Field(default=None, max_length=500, description="审核备注")


__all__ = [
    "ReportCreateReq",
    "ReportItem",
    "ReportListResp",
    "ReportReviewReq",
]
