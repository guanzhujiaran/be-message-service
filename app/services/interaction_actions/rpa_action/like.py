"""RPA 自定义操作（RPA_ACTION）点赞互动（2.47.0 对象化）。"""

from app.models.enums import InteractionBizTypeEnum
from app.services.interaction_actions.common.like import ResourceLikeAction


class LikeAction(ResourceLikeAction):
    """RPA 自定义操作点赞 / 取消点赞（幂等）。"""

    _biz_type = InteractionBizTypeEnum.RPA_ACTION
    error_messages = {
        "not_found": "RPA 操作不存在或暂不可互动",
        "invalid": "up 参数不合法（1=点赞, 2=取消点赞）",
    }


__all__ = ["LikeAction"]
