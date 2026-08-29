"""FastStream 消费者：消费 message 队列中的推送请求并分发到各渠道。

「推送」是消息系统（message-service）的第一个模块；后续评论 / 对话 / 私信等
模块将复用同一套 broker / 队列基础设施，按 routing_key 区分（如 message.push、
message.comment 等）。
"""

from faststream.rabbit import RabbitMessage
from loguru import logger

from app.models import PushMessagePayload
from app.services.message.external.push import PushMessageService
from app.services.message.external.push_helper import merge_config


async def handle_message(message: PushMessagePayload, _msg: RabbitMessage) -> None:
    """处理一条推送消息：构造配置 -> 调用 PushMessageService.send。

    用户信息已在投递前由 api 层拼进 message.title（标题前缀），
    消费者不再感知 user 字段，直接透传标题即可。

    推送失败**不重投**：`PushMessageService.send` 内部已按渠道降级并打 CRITICAL
    日志（【彻底推送失败】），此处捕获异常后正常返回，消息被 ack 丢弃。站外提醒
    属尽力而为，失败不补；避免失败消息被 NACK 反复重投直至死信。
    """
    config = merge_config(message)
    service = PushMessageService(config, push_type=message.push_type)
    try:
        await service.send(message.title, message.content)
    except Exception as e:  # noqa: BLE001
        logger.error(
            f"推送消费失败，消息丢弃不重投 title={message.title} "
            f"push_type={message.push_type}: {e}"
        )
