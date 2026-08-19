"""数据库表模型（SQLModel table=True）集中导出。

Alembic 的 env.py 与启动期 Schema 校验都从这里导入，
新增表模型时只要在本文件导出，迁移自动生成即可感知。
"""

from app.models.db.admin import MessageAdmin
from app.models.db.avatar_audit import TUserAvatarAudit
from app.models.db.ban import UserBan
from app.models.db.base import TimestampMixin
from app.models.db.comment import (
    CommentAction,
    CommentAt,
    CommentContent,
    CommentIndex,
    CommentReport,
    CommentSubject,
)
from app.models.db.dm import DmContentDeadLetter, DmMessageIndex, DmSession
from app.models.db.event import EventMessage, EventReadCursor
from app.models.db.favorite import TFavoriteFolder, TMomentFavorite, TUserFavoriteSetting
from app.models.db.follow import UserFollow
from app.models.db.interaction import TInteractionStat, TInteractionViewLog
from app.models.db.moment import (
    TMoment,
    TMomentAuditLog,
    TMomentLike,
    TMomentReport,
    TMomentStat,
    TMomentTopic,
    TMomentTopicRel,
    TMomentViewLog,
)
from app.models.db.notify import NotifyCursor, NotifyMessage, NotifyState
from app.models.db.report import TUserReport
from app.models.db.setting import UserActivity, UserMessageSetting

__all__ = [  # noqa: RUF022
    "CommentAction",
    "CommentAt",
    "CommentContent",
    "CommentIndex",
    # 评论
    "CommentReport",
    "CommentSubject",
    "TMoment",
    "TMomentStat",
    "TMomentLike",
    "TMomentTopic",
    "TMomentTopicRel",
    "TMomentViewLog",
    "TMomentReport",
    "TMomentAuditLog",
    # 收藏
    "TFavoriteFolder",
    "TMomentFavorite",
    "TUserFavoriteSetting",
    # 通用交互计数（2.17.0）
    "TInteractionStat",
    # 通用浏览去重（2.23.0）
    "TInteractionViewLog",
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
