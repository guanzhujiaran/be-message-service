"""MQ 消费层：各模块的 FastStream 消费处理函数。

| 模块     | 处理函数            | routing_key            |
| -------- | ------------------- | ---------------------- |
| 推送     | `handle_message`    | `message.push`         |
| 私信内容 | `handle_dm_content` | `message.dm.content`   |

站内信（系统通知 / 事件提醒 / 私信）的送达由数据库写路径保证，不在此处做任何
第三方渠道推送；第三方推送（PushMe / PushPlus 等）仅用于站外提醒类内容，
单独由 `handle_message` 处理。
"""

from app.consumers.comment import (
    handle_comment_audit,
    handle_comment_count,
    handle_comment_notify,
)
from app.consumers.dm import handle_dm_content
from app.consumers.push import handle_message

__all__ = [
    "handle_message",
    "handle_dm_content",
    "handle_comment_notify",
    "handle_comment_audit",
    "handle_comment_count",
]
