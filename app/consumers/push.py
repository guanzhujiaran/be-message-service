"""FastStream 消费者：消费 message 队列中的推送请求并分发到各渠道。

「推送」是消息系统（message-service）的第一个模块；后续评论 / 对话 / 私信等
模块将复用同一套 broker / 队列基础设施，按 routing_key 区分（如 message.push、
message.comment 等）。
"""

from faststream.rabbit import RabbitMessage

from app.models import PushMessagePayload
from app.services.push import PushMessageService
from app.services.push_helper import merge_config


async def handle_message(message: PushMessagePayload, _msg: RabbitMessage) -> None:
    """处理一条推送消息：构造配置 -> 调用 PushMessageService.send。

    用户信息已在投递前由 api 层拼进 message.title（标题前缀），
    消费者不再感知 user 字段，直接透传标题即可。

    处理失败直接抛错，由 subscriber 的 ack_policy=NACK_ON_ERROR 自动重回队列重试；
    不再内部 try/except 静默吞错。
    """
    config = merge_config(message)
    service = PushMessageService(config, push_type=message.push_type)
    await service.send(message.title, message.content)
