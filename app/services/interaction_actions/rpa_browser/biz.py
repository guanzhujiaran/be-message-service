"""RPA 浏览器实例（RPA_BROWSER）资源类（2.48.0）。"""

from app.models.enums import InteractionBizTypeEnum
from app.services.interaction_actions.common.generic_biz import GenericResourceBiz

__all__ = ["RpaBrowserBiz"]


class RpaBrowserBiz(GenericResourceBiz):
    """RPA 浏览器实例资源（行为同通用资源）。"""

    _biz_type = InteractionBizTypeEnum.RPA_BROWSER
