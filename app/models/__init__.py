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
    InteractionActionTypeEnum,
    MessageModuleEnum,
    NotifyLevelEnum,
    NotifyStatusEnum,
    NotifyTargetTypeEnum,
    InteractionBizTypeEnum,
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
    "DmMsgStatusEnum",
    "DmMsgTypeEnum",
    "DmRelationEnum",
    "DmSessionTypeEnum",
    "InteractionActionTypeEnum",
    "FeedbackRequest",
    "MessageModuleEnum",
    "MessageUser",
    "NotifyLevelEnum",
    "NotifyStatusEnum",
    "NotifyTargetTypeEnum",
    "PushChannelConfig",
    "InteractionBizTypeEnum",
    "PushMessage",
    "PushMessagePayload",
    "StandardResponse",
    "TestPushRequest",
    "TestPushResponse",
]
