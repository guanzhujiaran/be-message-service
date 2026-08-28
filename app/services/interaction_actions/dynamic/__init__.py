"""动态（`InteractionBizTypeEnum.DYNAMIC`）资源的互动操作子类集合（2.47.0）。

本包按 `biz_type` 组织：动态相关的互动操作子类（点赞 / 收藏 / 转发 / 分享 / 点踩 /
举报 / 浏览 / 审核通过 / 审核驳回）全部归属到 `dynamic/` 文件夹。基类
:class:`BaseInteractionAction` 与权限注册表仍位于上层
`app/services/interaction_actions/base.py`（通用，不绑定具体资源）。

新增其它 `biz_type` 的互动操作时，仿照本包另建对应文件夹（如 `lottery/`、`rpa_action/`…）。
"""

from app.services.interaction_actions.dynamic.audit import (
    AuditApproveAction,
    AuditRejectAction,
)
from app.services.interaction_actions.dynamic.dislike import DislikeAction
from app.services.interaction_actions.dynamic.favorite import FavoriteAction
from app.services.interaction_actions.dynamic.like import LikeAction
from app.services.interaction_actions.dynamic.report import ReportAction
from app.services.interaction_actions.dynamic.repost import RepostAction
from app.services.interaction_actions.dynamic.share import ShareAction
from app.services.interaction_actions.dynamic.view import ViewAction

__all__ = [
    "LikeAction",
    "FavoriteAction",
    "RepostAction",
    "ShareAction",
    "DislikeAction",
    "ReportAction",
    "ViewAction",
    "AuditApproveAction",
    "AuditRejectAction",
]
