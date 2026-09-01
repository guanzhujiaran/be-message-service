"""RPA 工作流（RPA_WORKFLOW）资源类（2.48.0）。"""

from app.models.enums import InteractionBizTypeEnum
from app.services.interaction_actions.common.generic_biz import GenericResourceBiz

__all__ = ["RpaWorkflowBiz"]


class RpaWorkflowBiz(GenericResourceBiz):
    """RPA 工作流资源（行为同通用资源）。"""

    _biz_type = InteractionBizTypeEnum.RPA_WORKFLOW
