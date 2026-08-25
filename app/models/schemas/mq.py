"""RabbitMQ 消息载体（与 HTTP 请求体解耦）。

所有异步链路的消息都定义在这里，routing_key 约定：

- `message.push`        → 外部渠道推送（原有能力）
- `message.dm.content`  → 私信内容异步落库（写性能天花板的关键）
- `message.dm.notify`   → 私信到达后的提醒推送
- `message.event.push`  → 事件提醒的实时推送
- `message.notify.push` → 系统通知的推送投递
"""

from sqlmodel import Field, SQLModel

from app.models.enums import DmMsgTypeEnum, EventTypeEnum


class DmContentPayload(SQLModel):
    """私信内容异步落库载体。

    发送私信时，索引 / 会话同步写主库（保证会话列表与消息列表立刻可见），
    正文则打包成本消息投递到 MQ，由消费者路由到月度库的分表写入。
    这样发送接口的 RT 不再受内容写入影响，抬高了写性能天花板。
    """

    msgkey: int = Field(description="消息全局唯一键，消费者据此解析分库分表")
    session_key: str = Field(description="会话键：小mid_大mid")
    sender_uid: int
    receiver_uid: int
    msg_type: DmMsgTypeEnum = DmMsgTypeEnum.TEXT
    content: str = ""
    msg_ts: int = Field(description="消息毫秒时间戳")


class DmNotifyPayload(SQLModel):
    """私信到达提醒（决定是否推送到外部渠道）。"""

    receiver_mid: int
    sender_mid: int
    sender_name: str | None = None
    preview: str = ""
    msgkey: int = 0
    is_stranger: bool = False


class EventPushPayload(SQLModel):
    """事件提醒的推送载体。"""

    event_id: int
    mid: int
    event_type: EventTypeEnum
    title: str = ""
    content: str = ""
    jump_url: str | None = None


class NotifyPushPayload(SQLModel):
    """系统通知的推送载体（按批投递，避免逐用户发 MQ）。"""

    notify_id: int
    title: str = ""
    content: str = ""
    jump_url: str | None = None
    mids: list[int] = Field(default_factory=list, description="本批目标用户")


class UserDeactivatePayload(SQLModel):
    """用户注销载体（异步执行完整删除流程）。

    注销接口只校验并投递本消息，由消费者异步物理删除 pptr 四表 + 彻底清除
    be-message 业务数据，避免接口同步阻塞删除大用户数据的耗时。
    """

    uid: int = Field(description="待注销用户 mid（>0）")


class InteractionViewPayload(SQLModel):
    """浏览统计载体（2.23.0；2.42.0 移除 refDate）。

    `/interaction/status` 接口对列表内每个资源组装本消息投递 `interaction.view` 队列，
    由消费者异步按 `bizType+bizId+mid`（每用户每资源一行）去重累计浏览数——
    主链路（互动态查询）不再同步写浏览，MQ 抖动 / 消费失败不影响 status 响应。
    浏览上报幂等：即使重试重复消费，ViewLog 唯一约束保证 Stat.viewCount 只首次 +1。
    """

    bizType: str = Field(description="资源类型（对外文字：dynamic/lottery/...）")
    bizId: str = Field(description="资源 id（字符串雪花 id）")
    mid: int = Field(description="浏览用户 mid")


__all__ = [
    "DmContentPayload",
    "DmNotifyPayload",
    "EventPushPayload",
    "InteractionViewPayload",
    "NotifyPushPayload",
    "UserDeactivatePayload",
]
