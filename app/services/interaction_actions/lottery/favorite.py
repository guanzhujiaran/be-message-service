"""抽奖卡片（LOTTERY）收藏互动（2.47.0 对象化）。"""

from app.models.enums import InteractionBizTypeEnum
from app.services.interaction_actions.common.favorite import ResourceFavoriteAction


class FavoriteAction(ResourceFavoriteAction):
    """抽奖卡片收藏 / 取消收藏（幂等；多夹 + 用户去重计数）。"""

    _biz_type = InteractionBizTypeEnum.LOTTERY
    error_messages = {
        "not_found": "抽奖卡片不存在",
        "folder_not_found": "收藏夹不存在",
        "invalid": "action 参数不合法（add/remove）",
    }


__all__ = ["FavoriteAction"]
