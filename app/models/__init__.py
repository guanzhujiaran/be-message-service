"""数据模型层。

分三层：

- `app.models.db`      —— SQLModel 表模型（table=True），由 Alembic 管理 Schema。
- `app.models.schemas` —— HTTP 请求 / 响应体与 MQ 载体，与表结构解耦。
- `app.models.enums`   —— 跨层共享的枚举。

历史的「推送」模块模型（push / user）保持原位置不变，向后兼容。
"""

from bili_common.models.response import StandardResponse

from app.models.enums import (
    DmMsgStatusEnum,
    DmMsgTypeEnum,
    DmRelationEnum,
    DmSessionTypeEnum,
    EventTypeEnum,
    MessageModuleEnum,
    NotifyLevelEnum,
    NotifyStatusEnum,
    NotifyTargetTypeEnum,
    SourceTypeEnum,
)
from app.models.push import (
    FeedbackRequest,
    PushChannelConfig,
    PushMessage,
    PushMessagePayload,
    TestPushRequest,
    TestPushResponse,
)
from app.models.user import MessageUser

__all__ = [
    "StandardResponse",
    "MessageUser",
    # 推送
    "PushChannelConfig",
    "PushMessage",
    "PushMessagePayload",
    "TestPushRequest",
    "TestPushResponse",
    "FeedbackRequest",
    # 枚举
    "MessageModuleEnum",
    "NotifyTargetTypeEnum",
    "NotifyStatusEnum",
    "NotifyLevelEnum",
    "EventTypeEnum",
    "SourceTypeEnum",
    "DmMsgTypeEnum",
    "DmMsgStatusEnum",
    "DmSessionTypeEnum",
    "DmRelationEnum",
]
