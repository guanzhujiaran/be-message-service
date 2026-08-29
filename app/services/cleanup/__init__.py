"""用户注销领域删除服务（`cleanup_*`）。

每个模块负责「按 uid 彻底清除」某一业务领域的全部数据，对外暴露
`delete_all_by_uid(session, uid)` 类方法；`PptrUser.deactivate` 只负责
按依赖顺序编排调用，不在此处放置具体 SQL。

| 模块            | 领域                                   | engine      |
| --------------- | -------------------------------------- | ----------- |
| `cleanup_moment`| 动态 `TMoment*` 及点赞/浏览痕迹        | be-message  |
| `cleanup_comment`| 评论 `msg_comment_*`                  | be-message  |
| `cleanup_follow`| 关注 / 拉黑 `msg_user_follow`          | be-message  |
| `cleanup_report`| 举报三表（动态/评论/用户）             | be-message  |
| `cleanup_favorite`| 收藏 `TFavorite*`                    | be-message  |
| `cleanup_dm`    | 私信 `msg_dm_*`                        | be-message  |
| `cleanup_notify`| 通知 `msg_notify_*`                    | be-message  |
| `cleanup_event` | 事件 `msg_event_*`                     | be-message  |
| `cleanup_misc`  | 设置 / 活跃 / 封禁 / 管理              | be-message  |
| `cleanup_pptr`  | pptr 四表 + 日志表                     | pptr PG     |
"""

from app.services.cleanup import (
    cleanup_comment,
    cleanup_dm,
    cleanup_event,
    cleanup_favorite,
    cleanup_follow,
    cleanup_misc,
    cleanup_moment,
    cleanup_notify,
    cleanup_pptr,
    cleanup_report,
)

__all__ = [
    "cleanup_comment",
    "cleanup_dm",
    "cleanup_event",
    "cleanup_favorite",
    "cleanup_follow",
    "cleanup_misc",
    "cleanup_moment",
    "cleanup_notify",
    "cleanup_pptr",
    "cleanup_report",
]
