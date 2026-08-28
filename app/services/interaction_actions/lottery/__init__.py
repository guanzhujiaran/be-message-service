"""抽奖卡片（`InteractionBizTypeEnum.LOTTERY`）的互动操作子类集合（2.47.0）。

资源存在性经抽奖 RPC 校验；互动实现复用 `common/` 通用点赞 / 点踩 / 收藏 / 分享 /
转发 / 浏览 / 举报，仅声明 `_biz_type = LOTTERY`。
"""

from app.services.interaction_actions.lottery.dislike import DislikeAction
from app.services.interaction_actions.lottery.favorite import FavoriteAction
from app.services.interaction_actions.lottery.like import LikeAction
from app.services.interaction_actions.lottery.report import ReportAction
from app.services.interaction_actions.lottery.repost import RepostAction
from app.services.interaction_actions.lottery.share import ShareAction
from app.services.interaction_actions.lottery.view import ViewAction

__all__ = [
    "LikeAction",
    "DislikeAction",
    "FavoriteAction",
    "ShareAction",
    "RepostAction",
    "ViewAction",
    "ReportAction",
]
