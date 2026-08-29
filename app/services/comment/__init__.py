"""评论服务。

评论的「写 / 读 / 互动 / 审核 / 审计」全部收敛在 `app.services.comment` 包下，
与站内信（`message.system`）、私信（`message.dm`）、外部推送（`message.pushme`）等服务解耦。

本包对外暴露统一公共 API，调用方既可 `from app.services.comment import CommentService`，
也可 `from app.services.comment.comment_read import CommentReadService`（子模块直引）。
"""

from app.services.comment.comment import (
    CommentService,
    VISIBLE_STATES,
    DEFAULT_REJECT_REASON,
    summarize_text,
)
from app.services.comment.comment_action import CommentActionService, compute_hot_score
from app.services.comment.comment_admin import CommentAdminService
from app.services.comment.comment_audit import audit_text
from app.services.comment.comment_read import CommentReadService

__all__ = [
    "CommentService",
    "VISIBLE_STATES",
    "DEFAULT_REJECT_REASON",
    "summarize_text",
    "CommentActionService",
    "compute_hot_score",
    "CommentAdminService",
    "audit_text",
    "CommentReadService",
]
