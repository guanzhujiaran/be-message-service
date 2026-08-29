"""「站外推送」RPC 服务端（be-message 为服务端）。

其它系统（be-gateway / RPA-Browser / be-bilibili-crawler 等）经 RabbitMQ
按 `message.push.rpc.<method>` 同步调用本模块，完成「站外提醒」类推送，
不再依赖 HTTP 网关转发与请求头注入的 `x-bili-*` 用户信息。

契约（方法名 / 请求 / 响应）统一来自 `bili_common.models.push_rpc`。
路由键前缀：`message.push.rpc.<method_name>`（见 PUSH_RPC_ROUTING_KEY_PREFIX）。
本模块只需被 main.py import 一次即可完成 RPC 注册（FastStream 全局 broker 单例）。

与 HTTP `/api/v1/message/push` 并存：HTTP 面向终端用户 / 浏览器侧，RPC 面向
服务端系统，二者都落到同一套 PushMessageService 执行体。
"""

from faststream.rabbit import RabbitQueue

from bili_common.models import (
    PushRpcSendNowParams,
    PushRpcSendNowResult,
    PushRpcSendParams,
    PushRpcSendResult,
    StandardResponse,
    push_rpc_routing_key_for,
    success_response,
    error_response,
)
from bili_common.models.push_rpc import PushRpcMethodName
from bili_common.rpc.safe import rpc_safe

from app.core.broker import broker, message_exchange, message_queue
from app.core.broker import RK_PUSH
from app.models import PushMessagePayload
from app.services.message.external.push import PushMessageService
from app.services.message.external.push_helper import merge_config


def _with_label(title: str, user_label: str | None) -> str:
    """若提供了来源标签，则拼进标题前缀，便于区分推送来源（与 HTTP 层一致）。"""
    label = (user_label or "").strip()
    if not label:
        return title
    return f"[{label}] {title}"


@broker.subscriber(
    queue=RabbitQueue(
        push_rpc_routing_key_for(PushRpcMethodName.PUSH_MESSAGE),
        routing_key=push_rpc_routing_key_for(PushRpcMethodName.PUSH_MESSAGE),
        durable=True,
    ),
    exchange=message_exchange,
)
@rpc_safe
async def rpc_push_message(params: PushRpcSendParams) -> StandardResponse:
    """投递推送消息到队列（push_message，异步）。

    等价 HTTP `POST /api/v1/message/push`：把消息投递到 `message.push` 队列，
    由消费者异步分发到各渠道，不阻塞调用方。
    """
    title = _with_label(params.title, params.user_label)
    payload = PushMessagePayload(
        title=title,
        content=params.content,
        push_type=params.push_type,
        config=params.config,
    )
    try:
        await broker.publish(
            message=payload.model_dump(),
            exchange=message_exchange,
            routing_key=RK_PUSH,
            queue=message_queue,
        )
    except Exception as e:  # noqa: BLE001
        return error_response(code=500, msg=f"发布推送消息失败: {e}")
    return success_response(data=PushRpcSendResult(title=title, queued=True))


@broker.subscriber(
    queue=RabbitQueue(
        push_rpc_routing_key_for(PushRpcMethodName.SEND_PUSH_NOW),
        routing_key=push_rpc_routing_key_for(PushRpcMethodName.SEND_PUSH_NOW),
        durable=True,
    ),
    exchange=message_exchange,
)
@rpc_safe
async def rpc_send_push_now(params: PushRpcSendNowParams) -> StandardResponse:
    """立即发送推送（send_push_now，同步）。

    等价 HTTP `POST /api/v1/message/push/test`：同步执行渠道降级分发，
    调用方需等待渠道返回结果；适合测试 / 低频即时提醒场景。
    """
    title = _with_label(params.title, params.user_label)
    merged = merge_config(
        PushMessagePayload(
            title=title,
            content=params.content,
            push_type=params.push_type,
            config=params.config,
        )
    )
    service = PushMessageService(merged, push_type=params.push_type)
    try:
        sent = await service.send(title, params.content)
    except Exception as e:  # noqa: BLE001
        return error_response(
            code=500,
            msg=f"立即推送失败: {e}",
            data=PushRpcSendNowResult(success=False, message=str(e)),
        )
    if not sent:
        return success_response(
            data=PushRpcSendNowResult(
                success=False,
                message="无可用推送渠道，请检查通知配置（MESSAGE_CONFIG 或传入的 config）",
            )
        )
    return success_response(
        data=PushRpcSendNowResult(success=True, message="推送已发送，请检查对应渠道是否收到")
    )


__all__ = [
    "rpc_push_message",
    "rpc_send_push_now",
]
