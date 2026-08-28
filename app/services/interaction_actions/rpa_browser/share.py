"""RPA 浏览器实例（RPA_BROWSER）分享上报互动（2.47.0 对象化）。"""

from app.models.enums import InteractionBizTypeEnum
from app.services.interaction_actions.common.share import ResourceShareAction


class ShareAction(ResourceShareAction):
    """RPA 浏览器实例分享上报（行为上报，不幂等）。"""

    _biz_type = InteractionBizTypeEnum.RPA_BROWSER


__all__ = ["ShareAction"]
