"""抽奖卡片（LOTTERY）举报互动（2.47.0 对象化）。"""

from app.models.enums import InteractionBizTypeEnum
from app.services.interaction_actions.common.report import ResourceReportAction


class ReportAction(ResourceReportAction):
    """抽奖卡片举报（幂等；资源存在性经抽奖 RPC 校验）。"""

    _biz_type = InteractionBizTypeEnum.LOTTERY


__all__ = ["ReportAction"]
