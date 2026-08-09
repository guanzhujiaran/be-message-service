"""数据库表模型（SQLModel table=True）集中导出。

Alembic 的 env.py 与启动期 Schema 校验都从这里导入，
新增表模型时只要在本文件导出，迁移自动生成即可感知。
"""

from app.models.db.admin import MessageAdmin
from app.models.db.ban import UserBan
from app.models.db.base import TimestampMixin
from app.models.db.comment import (
    CommentAction,
    CommentAt,
    CommentContent,
    CommentIndex,
    CommentSubject,
)
from app.models.db.dm import DmContentDeadLetter, DmMessageIndex, DmSession
from app.models.db.event import EventMessage, EventReadCursor
from app.models.db.follow import UserFollow
from app.models.db.notify import NotifyCursor, NotifyMessage, NotifyState
from app.models.db.setting import UserActivity, UserMessageSetting

__all__ = [
    "CommentAction",
    "CommentAt",
    "CommentContent",
    "CommentIndex",
    # 评论
    "CommentSubject",
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
    "UserActivity",
    # 用户封禁（审核联动）
    "UserBan",
    # 用户关注 / 拉黑
    "UserFollow",
    # 设置与活跃度
    "UserMessageSetting",
]
