"""业务服务层。

| 模块                | 归属   | 职责                                                   |
| ------------------- | ------ | ------------------------------------------------------ |
| `insite.notify`     | 站内   | 系统通知：发布 / 拉取（游标去重）/ 已读                 |
| `insite.event`      | 站内   | 事件提醒：上报（幂等）/ 聚合展示 / 已读                 |
| `dm`                | 站内   | 私信：写扩散发送、会话列表、聊天记录、删除撤回          |
| `dm_content`        | 站内   | 私信正文的月度分库分表读写                              |
| `insite.setting`    | 站内   | 站内消息设置（整个系统的第一道闸门，被站内模块读取）    |
| `insite.activity`   | 站内   | 用户活跃度与推送策略分流（实时 / 批量 / 跳过）          |
| `publisher`         | 共用   | 统一的 MQ 投递封装                                      |
| `external`          | 站外   | 站外渠道推送（PushMe / PushPlus / SMTP 等）发送实现      |
| `push_helper`       | 站外   | 站外推送业务辅助逻辑（渠道配置合并、用户标签）          |
"""

import app.services.message.infrastructure.publisher as publisher
from app.services.message.insite.activity import ActivityService
from app.services.message.dm.dm import (
    DmInbox,
    DmSessionObject,
    make_session_key,
    mark_content_ready,
    retry_dead_letters,
)
from app.services.message.dm.dm_content import DmContentService
from app.services.message.insite.events import build_dedup_key
from app.services.message.insite.notify import NotifyService
from app.services.message.insite.setting import SettingService

__all__ = [
    "ActivityService",
    "DmContentService",
    "DmInbox",
    "DmSessionObject",
    "NotifyService",
    "SettingService",
    "build_dedup_key",
    "make_session_key",
    "mark_content_ready",
    "publisher",
    "retry_dead_letters",
]
