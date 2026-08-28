"""RPA 插件（RPA_PLUGIN）浏览上报互动（2.47.0 对象化）。"""

from app.models.enums import InteractionBizTypeEnum
from app.services.interaction_actions.common.view import ResourceViewAction


class ViewAction(ResourceViewAction):
    """RPA 插件浏览上报（弱依赖计数，不校验资源存在）。"""

    _biz_type = InteractionBizTypeEnum.RPA_PLUGIN


__all__ = ["ViewAction"]
