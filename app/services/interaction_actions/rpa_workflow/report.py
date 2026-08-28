"""RPA 工作流（RPA_WORKFLOW）举报互动（2.47.0 对象化）。"""

from app.models.enums import InteractionBizTypeEnum
from app.services.interaction_actions.common.report import ResourceReportAction


class ReportAction(ResourceReportAction):
    """RPA 工作流举报（幂等）。"""

    _biz_type = InteractionBizTypeEnum.RPA_WORKFLOW


__all__ = ["ReportAction"]
