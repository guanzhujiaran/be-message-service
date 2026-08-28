"""抽奖卡片（LOTTERY）点踩互动（2.47.0 对象化）。"""

from app.models.enums import InteractionBizTypeEnum
from app.services.interaction_actions.common.dislike import ResourceDislikeAction


class DislikeAction(ResourceDislikeAction):
    """抽奖卡片点踩 / 取消点踩（幂等；资源存在性经抽奖 RPC 校验）。"""

    _biz_type = InteractionBizTypeEnum.LOTTERY


__all__ = ["DislikeAction"]
