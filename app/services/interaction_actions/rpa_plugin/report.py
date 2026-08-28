"""RPA 插件（RPA_PLUGIN）举报互动（2.47.0 对象化）。"""

from app.models.enums import InteractionBizTypeEnum
from app.services.interaction_actions.common.report import ResourceReportAction


class ReportAction(ResourceReportAction):
    """RPA 插件举报（幂等）。"""

    _biz_type = InteractionBizTypeEnum.RPA_PLUGIN


__all__ = ["ReportAction"]
