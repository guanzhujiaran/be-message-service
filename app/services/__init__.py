"""业务服务层。

| 模块           | 职责                                                          |
| -------------- | ------------------------------------------------------------- |
| `notify`       | 系统通知：发布 / 拉取（游标去重）/ 已读                        |
| `event`        | 事件提醒：上报（幂等）/ 聚合展示 / 已读                        |
| `dm`           | 私信：写扩散发送、会话列表、聊天记录、删除撤回                 |
| `dm_content`   | 私信正文的月度分库分表读写                                     |
| `setting`      | 消息设置（整个系统的第一道闸门）                               |
| `activity`     | 用户活跃度与推送策略分流（实时 / 批量 / 跳过）                 |
| `publisher`    | 统一的 MQ 投递封装                                             |
| `push`         | 外部推送渠道（PushMe / PushPlus / SMTP）发送实现               |
| `push_helper`  | 推送业务辅助逻辑（渠道配置合并、用户标签）                     |
"""

from app.services import publisher
from app.services.activity import ActivityService
from app.services.dm import DmService, make_session_key
from app.services.dm_content import DmContentService
from app.services.event import EventService, build_dedup_key
from app.services.notify import NotifyService
from app.services.setting import SettingService

__all__ = [
    "NotifyService",
    "EventService",
    "build_dedup_key",
    "DmService",
    "make_session_key",
    "DmContentService",
    "SettingService",
    "ActivityService",
    "publisher",
]
