"""RPA 插件（`InteractionBizTypeEnum.RPA_PLUGIN`）的互动操作子类集合（2.47.0）。

资源存在性校验器未注册（默认放行，由调用方/前端保证）；互动实现复用 `common/` 通用
点赞 / 点踩 / 收藏 / 分享 / 转发 / 浏览 / 举报，仅声明 `_biz_type = RPA_PLUGIN`。
"""

from app.services.interaction_actions.rpa_plugin.dislike import DislikeAction
from app.services.interaction_actions.rpa_plugin.favorite import FavoriteAction
from app.services.interaction_actions.rpa_plugin.like import LikeAction
from app.services.interaction_actions.rpa_plugin.report import ReportAction
from app.services.interaction_actions.rpa_plugin.repost import RepostAction
from app.services.interaction_actions.rpa_plugin.share import ShareAction
from app.services.interaction_actions.rpa_plugin.view import ViewAction

__all__ = [
    "LikeAction",
    "DislikeAction",
    "FavoriteAction",
    "ShareAction",
    "RepostAction",
    "ViewAction",
    "ReportAction",
]
