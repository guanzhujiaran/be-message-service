"""RPA 自定义操作（RPA_ACTION）分享上报互动（2.47.0 对象化）。"""

from app.models.enums import InteractionBizTypeEnum
from app.services.interaction_actions.common.share import ResourceShareAction


class ShareAction(ResourceShareAction):
    """RPA 自定义操作分享上报（行为上报，不幂等）。"""

    _biz_type = InteractionBizTypeEnum.RPA_ACTION


__all__ = ["ShareAction"]
