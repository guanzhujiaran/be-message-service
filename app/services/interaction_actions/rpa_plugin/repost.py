"""RPA 插件（RPA_PLUGIN）转发 / attach 互动（2.47.0 对象化）。"""

from app.models.enums import InteractionBizTypeEnum
from app.services.interaction_actions.common.repost import ResourceRepostAction


class RepostAction(ResourceRepostAction):
    """RPA 插件转发 / attach（attach 计数 repostCount +1）。"""

    _biz_type = InteractionBizTypeEnum.RPA_PLUGIN


__all__ = ["RepostAction"]
