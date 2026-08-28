"""RPA 工作流（RPA_WORKFLOW）转发 / attach 互动（2.47.0 对象化）。"""

from app.models.enums import InteractionBizTypeEnum
from app.services.interaction_actions.common.repost import ResourceRepostAction


class RepostAction(ResourceRepostAction):
    """RPA 工作流转发 / attach（attach 计数 repostCount +1）。"""

    _biz_type = InteractionBizTypeEnum.RPA_WORKFLOW


__all__ = ["RepostAction"]
