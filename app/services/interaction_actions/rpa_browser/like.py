"""RPA 浏览器实例（RPA_BROWSER）点赞互动（2.47.0 对象化）。"""

from app.models.enums import InteractionBizTypeEnum
from app.services.interaction_actions.common.like import ResourceLikeAction


class LikeAction(ResourceLikeAction):
    """RPA 浏览器实例点赞 / 取消点赞（幂等）。"""

    _biz_type = InteractionBizTypeEnum.RPA_BROWSER
    error_messages = {
        "not_found": "RPA 浏览器实例不存在或暂不可互动",
        "invalid": "up 参数不合法（1=点赞, 2=取消点赞）",
    }


__all__ = ["LikeAction"]
