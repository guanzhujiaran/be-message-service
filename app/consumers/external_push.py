"""FastStream 消费者：消费 message 队列中的推送请求并分发到各渠道。

「推送」是消息系统（message-service）的第一个模块；后续评论 / 对话 / 私信等
模块将复用同一套 broker / 队列基础设施，按 routing_key 区分（如 message.push、
message.comment 等）。

治理策略见 ``app/services/message/external/push_aggregator.py``：
失败主题（标题以 ``[f]`` 开头）走「首条直推 + 冷却期内聚合」，按
「接收人（``merge_config`` 合并后生效的 push config 摘要）× 标题」分桶 ——
同一个 push config 的同类失败会合成一条摘要；业务通知（``[i]``/``[s]``/``[w]``）
与 per-user 推送仍是 1:1 直推。
"""

from faststream.rabbit import RabbitMessage

from app.models import PushMessagePayload
from app.services.message.external.push_aggregator import deliver


async def handle_message(message: PushMessagePayload, _msg: RabbitMessage) -> None:
    """处理一条推送消息：按聚合策略直推或缓冲，缓冲的由到期扫描统一发摘要。

    推送失败**不重投**：``deliver`` 内部已按渠道降级并打 CRITICAL 日志
    （【彻底推送失败】），此处不抛异常，消息被 ack 丢弃。站外提醒属尽力而为，
    失败不补；避免失败消息被 NACK 反复重投直至死信。
    """
    await deliver(message)
