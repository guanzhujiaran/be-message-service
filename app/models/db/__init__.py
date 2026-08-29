"""数据库表模型（SQLModel table=True）集中导出。

Alembic 的 env.py 与启动期 Schema 校验都从这里导入，
新增表模型时只要在本文件导出，迁移自动生成即可感知。
"""

from app.models.db.admin_tbl import MessageAdmin
from app.models.db.avatar_audit_tbl import TUserAvatarAudit
from app.models.db.ban_tbl import UserBan
from app.models.db.base_tbl import TimestampMixin
from app.models.db.comment_tbl import (
    CommentAction,
    CommentAt,
    CommentContent,
    CommentIndex,
    CommentReport,
    CommentSubject,
)
from app.models.db.dm_tbl import DmContentDeadLetter, DmMessageIndex, DmSession
from app.models.db.event_tbl import EventMessage, EventReadCursor
from app.models.db.favorite_tbl import TFavoriteFolder, TMomentFavorite, TUserFavoriteSetting
from app.models.db.folder_cover_audit_tbl import TFolderCoverAudit
from app.models.db.follow_tbl import UserFollow
from app.models.db.interaction_tbl import TInteractionStat, TInteractionViewLog
from app.models.db.moment_tbl import (
    MomentAuthorQuality,
    TMoment,
    TMomentAuditLog,
    TMomentDislike,
    TMomentLike,
    TResourceReport,
    TMomentTopic,
    TMomentTopicRel,
)
from app.models.db.notify_tbl import NotifyCursor, NotifyMessage, NotifyState
from app.models.db.resource_feed_tbl import TResourceFeed
from app.models.db.report_tbl import TUserReport
from app.models.db.setting_tbl import UserActivity, UserMessageSetting

__all__ = [  # noqa: RUF022
    "CommentAction",
    "CommentAt",
    "CommentContent",
    "CommentIndex",
    # 评论
    "CommentReport",
    "CommentSubject",
    "TMoment",
    "TMomentLike",
    "TMomentDislike",
    "MomentAuthorQuality",
    "TMomentTopic",
    "TMomentTopicRel",
    "TResourceReport",
    "TMomentAuditLog",
    # 收藏
    "TFavoriteFolder",
    "TMomentFavorite",
    "TUserFavoriteSetting",
    # 通用交互计数（2.17.0；2.36.0 起动态并入）
    "TInteractionStat",
    # 通用浏览去重（2.23.0）
    "TInteractionViewLog",
    # 通用资源 Feed 元数据（2.36.0）
    "TResourceFeed",
    "DmContentDeadLetter",
    "DmMessageIndex",
    # 私信
    "DmSession",
    # 事件提醒
    "EventMessage",
    "EventReadCursor",
    # 消息管理端权限
    "MessageAdmin",
    "NotifyCursor",
    # 系统通知
    "NotifyMessage",
    "NotifyState",
    "TimestampMixin",
    # 头像更换审核
    "TUserAvatarAudit",
    # 收藏夹封面审核（2.28.0）
    "TFolderCoverAudit",
    # 统一举报（2.14.0）
    "TUserReport",
    "UserActivity",
    # 用户封禁（审核联动）
    "UserBan",
    # 用户关注 / 拉黑
    "UserFollow",
    # 设置与活跃度
    "UserMessageSetting",
]
