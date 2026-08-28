"""RPA 自定义操作（RPA_ACTION）浏览上报互动（2.47.0 对象化）。"""

from app.models.enums import InteractionBizTypeEnum
from app.services.interaction_actions.common.view import ResourceViewAction


class ViewAction(ResourceViewAction):
    """RPA 自定义操作浏览上报（弱依赖计数，不校验资源存在）。"""

    _biz_type = InteractionBizTypeEnum.RPA_ACTION


__all__ = ["ViewAction"]
