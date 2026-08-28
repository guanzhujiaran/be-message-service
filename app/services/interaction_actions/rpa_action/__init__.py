"""RPA 自定义操作（`InteractionBizTypeEnum.RPA_ACTION`）的互动操作子类集合（2.47.0）。

资源存在性校验器未注册（默认放行，由调用方/前端保证）；互动实现复用 `common/` 通用
点赞 / 点踩 / 收藏 / 分享 / 转发 / 浏览 / 举报，仅声明 `_biz_type = RPA_ACTION`。
"""

from app.services.interaction_actions.rpa_action.dislike import DislikeAction
from app.services.interaction_actions.rpa_action.favorite import FavoriteAction
from app.services.interaction_actions.rpa_action.like import LikeAction
from app.services.interaction_actions.rpa_action.report import ReportAction
from app.services.interaction_actions.rpa_action.repost import RepostAction
from app.services.interaction_actions.rpa_action.share import ShareAction
from app.services.interaction_actions.rpa_action.view import ViewAction

__all__ = [
    "LikeAction",
    "DislikeAction",
    "FavoriteAction",
    "ShareAction",
    "RepostAction",
    "ViewAction",
    "ReportAction",
]
