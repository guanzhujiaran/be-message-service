"""RPA 浏览器实例（RPA_BROWSER）举报互动（2.47.0 对象化）。"""

from app.models.enums import InteractionBizTypeEnum
from app.services.interaction_actions.common.report import ResourceReportAction


class ReportAction(ResourceReportAction):
    """RPA 浏览器实例举报（幂等）。"""

    _biz_type = InteractionBizTypeEnum.RPA_BROWSER


__all__ = ["ReportAction"]
