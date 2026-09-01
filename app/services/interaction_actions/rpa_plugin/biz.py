"""RPA 插件（RPA_PLUGIN）资源类（2.48.0）。"""

from app.models.enums import InteractionBizTypeEnum
from app.services.interaction_actions.common.generic_biz import GenericResourceBiz

__all__ = ["RpaPluginBiz"]


class RpaPluginBiz(GenericResourceBiz):
    """RPA 插件资源（行为同通用资源）。"""

    _biz_type = InteractionBizTypeEnum.RPA_PLUGIN
