"""抽奖卡片（LOTTERY）点赞互动（2.47.0 对象化）。"""

from app.models.enums import InteractionBizTypeEnum
from app.services.interaction_actions.common.like import ResourceLikeAction


class LikeAction(ResourceLikeAction):
    """抽奖卡片点赞 / 取消点赞（幂等；资源存在性经抽奖 RPC 校验）。"""

    _biz_type = InteractionBizTypeEnum.LOTTERY
    error_messages = {
        "not_found": "抽奖卡片不存在或暂不可互动",
        "invalid": "up 参数不合法（1=点赞, 2=取消点赞）",
    }


__all__ = ["LikeAction"]
