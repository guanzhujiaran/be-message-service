"""RPA 浏览器实例（RPA_BROWSER）转发 / attach 互动（2.47.0 对象化）。"""

from app.models.enums import InteractionBizTypeEnum
from app.services.interaction_actions.common.repost import ResourceRepostAction


class RepostAction(ResourceRepostAction):
    """RPA 浏览器实例转发 / attach（attach 计数 repostCount +1）。"""

    _biz_type = InteractionBizTypeEnum.RPA_BROWSER


__all__ = ["RepostAction"]
