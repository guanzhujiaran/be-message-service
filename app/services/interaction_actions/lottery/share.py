"""抽奖卡片（LOTTERY）分享上报互动（2.47.0 对象化）。"""

from app.models.enums import InteractionBizTypeEnum
from app.services.interaction_actions.common.share import ResourceShareAction


class ShareAction(ResourceShareAction):
    """抽奖卡片分享上报（行为上报，不幂等）。"""

    _biz_type = InteractionBizTypeEnum.LOTTERY


__all__ = ["ShareAction"]
