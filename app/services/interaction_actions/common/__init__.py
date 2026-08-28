"""通用互动实现（2.47.0）：供非动态 `biz_type`（lottery / rpa_*）复用。

- `ResourceLikeAction`：通用点赞（注册式校验器 + `TMomentLike` 明细 + `TInteractionStat` 计数）；
- `ResourceFavoriteAction`：通用收藏（校验器 + 多夹 + 用户去重计数）；
- `ResourceDislikeAction`：通用点踩（校验器 + `TMomentDislike` 明细 + 计数）；
- `ResourceShareAction`：通用分享上报（校验器 + `shareCount` +1）；
- `ResourceViewAction`：通用浏览上报（弱依赖计数，不校验资源存在）；
- `ResourceReportAction`：通用举报（校验器 + 统一举报记录）。

各 `biz_type` 文件夹内的子类只需继承对应通用实现并声明自己的 `_biz_type`。
"""

from app.services.interaction_actions.common.dislike import ResourceDislikeAction
from app.services.interaction_actions.common.favorite import ResourceFavoriteAction
from app.services.interaction_actions.common.like import ResourceLikeAction
from app.services.interaction_actions.common.report import ResourceReportAction
from app.services.interaction_actions.common.repost import ResourceRepostAction
from app.services.interaction_actions.common.share import ResourceShareAction
from app.services.interaction_actions.common.view import ResourceViewAction

__all__ = [
    "ResourceLikeAction",
    "ResourceFavoriteAction",
    "ResourceDislikeAction",
    "ResourceShareAction",
    "ResourceRepostAction",
    "ResourceViewAction",
    "ResourceReportAction",
]
