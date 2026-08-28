"""RPA 浏览器实例（RPA_BROWSER）浏览上报互动（2.47.0 对象化）。"""

from app.models.enums import InteractionBizTypeEnum
from app.services.interaction_actions.common.view import ResourceViewAction


class ViewAction(ResourceViewAction):
    """RPA 浏览器实例浏览上报（弱依赖计数，不校验资源存在）。"""

    _biz_type = InteractionBizTypeEnum.RPA_BROWSER


__all__ = ["ViewAction"]
