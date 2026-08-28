"""RPA 自定义操作（RPA_ACTION）点踩互动（2.47.0 对象化）。"""

from app.models.enums import InteractionBizTypeEnum
from app.services.interaction_actions.common.dislike import ResourceDislikeAction


class DislikeAction(ResourceDislikeAction):
    """RPA 自定义操作点踩 / 取消点踩（幂等）。"""

    _biz_type = InteractionBizTypeEnum.RPA_ACTION


__all__ = ["DislikeAction"]
