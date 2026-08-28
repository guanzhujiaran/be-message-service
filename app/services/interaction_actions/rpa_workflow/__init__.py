"""RPA 工作流（`InteractionBizTypeEnum.RPA_WORKFLOW`）的互动操作子类集合（2.47.0）。

资源存在性校验器未注册（默认放行，由调用方/前端保证）；互动实现复用 `common/` 通用
点赞 / 点踩 / 收藏 / 分享 / 转发 / 浏览 / 举报，仅声明 `_biz_type = RPA_WORKFLOW`。
"""

from app.services.interaction_actions.rpa_workflow.dislike import DislikeAction
from app.services.interaction_actions.rpa_workflow.favorite import FavoriteAction
from app.services.interaction_actions.rpa_workflow.like import LikeAction
from app.services.interaction_actions.rpa_workflow.report import ReportAction
from app.services.interaction_actions.rpa_workflow.repost import RepostAction
from app.services.interaction_actions.rpa_workflow.share import ShareAction
from app.services.interaction_actions.rpa_workflow.view import ViewAction

__all__ = [
    "LikeAction",
    "DislikeAction",
    "FavoriteAction",
    "ShareAction",
    "RepostAction",
    "ViewAction",
    "ReportAction",
]
