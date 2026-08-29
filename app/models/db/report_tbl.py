"""举报 ORM 模型（be-message MySQL 主库 `BiliMessageDB`）。

举报记录统一结构继承 bili-common 的 `ReportBase`（同构字段）：
- `TUserReport`：用户空间举报（bizType=user，bizId=mid）

与资源 `TResourceReport` / 评论 `CommentReport` 结构一致，通用逻辑由
`bili_common.services.report.ReportBaseService` 统一实现（幂等写 / 列表 / 审核）。

> 说明：举报按业务归属各自系统（RPA 举报在 RPA 库、动态/评论/用户举报在 be-message），
> 通用逻辑共用 bili-common。
"""

from sqlalchemy import UniqueConstraint
from sqlmodel import Index

from bili_common.models.report import ReportBase


class TUserReport(ReportBase, table=True):
    """用户空间举报表（继承 bili-common `ReportBase`；bizType=user，bizId=mid）。

    幂等：`ReportBaseService.record_report` 按 (reportMid, bizType, bizId) 去重；
    业务唯一约束 `TUserReport_reportMid_bizType_bizId_key` 兜底。
    """

    __tablename__ = "TUserReport"
    __table_args__ = (
        UniqueConstraint(
            "reportMid", "bizType", "bizId",
            name="TUserReport_reportMid_bizType_bizId_key",
        ),
        Index("idx_tuser_report_biz", "bizType", "bizId"),
        {"extend_existing": True, "comment": "用户空间举报表：bizType=user，bizId=mid（继承 ReportBase）"},
    )


__all__ = ["TUserReport"]
