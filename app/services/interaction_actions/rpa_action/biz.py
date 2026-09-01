"""RPA 自定义操作（RPA_ACTION）资源类（2.48.0）。"""

from app.models.enums import InteractionBizTypeEnum
from app.services.interaction_actions.common.generic_biz import GenericResourceBiz

__all__ = ["RpaActionBiz"]


class RpaActionBiz(GenericResourceBiz):
    """RPA 自定义操作资源（行为同通用资源）。"""

    _biz_type = InteractionBizTypeEnum.RPA_ACTION
