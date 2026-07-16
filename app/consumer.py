"""FastStream 消费者：消费 message 队列中的推送请求并分发到各渠道。

「推送」是消息系统（message-service）的第一个模块；后续评论 / 对话 / 私信等
模块将复用同一套 broker / 队列基础设施，按 routing_key 区分（如 message.push、
message.comment 等）。
"""

import traceback

from faststream.rabbit import RabbitMessage
from loguru import logger

from app.config import settings
from app.models import MessageUser, PushChannelConfig, PushMessage
from app.services.push import PushMessageService


def _is_set(value) -> bool:
    """判断某字段值是否「有效可覆盖」。

    空值（None / "" / 0 / 空列表）以及模板占位符（形如 ``<SMTP_SERVER>``、
    ``<PUSHME_KEY>`` 的 ``<...>`` 字符串）均视为「未设置」，不应覆盖全局配置。
    防止上游（如 dev 环境）把未替换的占位符带入消息，污染全局兜底配置。
    """
    if value in (None, "", 0, []):
        return False
    if isinstance(value, str) and value.startswith("<") and value.endswith(">"):
        return False
    return True


def _merge_config(message: PushMessage) -> PushChannelConfig:
    """合并全局环境变量配置与消息内携带的 per-user 配置。

    以全局 message_config（纯 pydantic 模型）为基准，消息内 config 优先级更高
    （仅覆盖有效非空的字段，模板占位符 ``<...>`` 视为未设置，不覆盖）。
    """
    merged = settings.message_config.model_dump()
    if message.config is not None:
        for field_name, value in message.config.model_dump().items():
            if _is_set(value):
                merged[field_name] = value
    return PushChannelConfig(**merged)


def format_user_label(user: MessageUser | None) -> str:
    """根据用户信息生成用于推送标题前缀的标签，例如「用户 123456（星瞳）」。

    无用户信息时返回空字符串（调用方据此决定是否加前缀）。
    """
    if not user or not (user.mid and str(user.mid).strip()):
        return ""
    label = f"用户 {user.mid.strip()}"
    name = user.user_name or user.uname
    if name and str(name).strip():
        label += f"（{str(name).strip()}）"
    return label


async def handle_message(message: PushMessage, msg: RabbitMessage) -> None:
    """处理一条推送消息：构造配置 -> 调用 PushMessageService.send。"""
    try:
        config = _merge_config(message)
        service = PushMessageService(config, push_type=message.push_type)
        # 将上游透传的用户信息写入推送标题，便于区分推送来源
        title = message.title
        label = format_user_label(message.user)
        if label:
            title = f"[{label}] {title}"
        await service.send(title, message.content)
        await msg.ack()
    except Exception as e:  # noqa: BLE001
        logger.error(
            f"推送消息处理失败: {e}\n"
            f"title={message.title}\n{traceback.format_exc()}"
        )
        # 不重新入队，避免失败消息导致的死循环
        await msg.nack(requeue=False)
