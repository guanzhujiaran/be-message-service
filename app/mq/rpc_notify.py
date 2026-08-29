"""「系统通知」RPC 服务端（be-message 为服务端，2.48.0）。

其它系统（be-gateway / RPA-Browser / be-bilibili-crawler 等）经 RabbitMQ
按 `message.notify.rpc.<method>` 同步调用本模块，发布站内系统通知
（写入 `msg_notify`），不再依赖 HTTP 网关转发与请求头注入的 `x-bili-*` 用户信息。

契约（方法名 / 请求 / 响应）统一来自 `bili_common.rpc.notify`。
路由键前缀：`message.notify.rpc.<method_name>`（见 NOTIFY_RPC_ROUTING_KEY_PREFIX）。
本模块只需被 main.py import 一次即可完成 RPC 注册（FastStream 全局 broker 单例）。

与管理端 HTTP `POST /api/v1/message/notify/admin/create` 并存且落到同一个
执行体：HTTP 面向管理员浏览器侧，RPC 面向服务端系统。区别是 RPC 走
`NotifyService.create_idempotent`（CUSTOM 单人场景按 `(target_value, title)`
判重），避免调用方超时重试导致重复通知用户。

系统内部触发的通知（如新用户欢迎）直接调服务层，不走 RPC 自调用。
"""

from faststream.rabbit import RabbitQueue

from bili_common.models import (
    StandardResponse,
    error_response,
    notify_rpc_routing_key_for,
    success_response,
)
from bili_common.models.notify_rpc import (
    NotifyRpcMethodName,
    PublishNotifyParams,
    PublishNotifyResult,
)
from bili_common.rpc.safe import rpc_safe

from app.core.broker import broker, message_exchange
from app.core.database import new_session
from app.models.schemas import NotifyCreateReq
from app.services.message.insite.notify import NotifyService


@broker.subscriber(
    queue=RabbitQueue(
        notify_rpc_routing_key_for(NotifyRpcMethodName.PUBLISH_NOTIFY),
        routing_key=notify_rpc_routing_key_for(NotifyRpcMethodName.PUBLISH_NOTIFY),
        durable=True,
    ),
    exchange=message_exchange,
)
@rpc_safe
async def rpc_publish_notify(params: PublishNotifyParams) -> StandardResponse:
    """发布系统通知（publish_notify，同步写库）。

    等价管理端 HTTP `POST /api/v1/message/notify/admin/create`：写入
    `msg_notify` 一行，随后由既有 `notify_push` 链路异步投递到用户会话。

    幂等：CUSTOM + 单个 mid 时按 `(target_value, title)` 判重，重复调用返回
    既有 `notify_id`（`duplicated=True`），不会重复通知用户。
    """
    req = NotifyCreateReq(
        title=params.title,
        content=params.content,
        jump_url=params.jump_url,
        target_type=params.target_type,
        target_value=params.target_value,
        level=params.level,
        publish_now=params.publish_now,
    )
    async with new_session() as session:
        item, duplicated = await NotifyService.create_idempotent(
            session, params.creator_mid, req
        )
    if item is None:
        # 用户关闭了系统通知（recv_notify=False）而被跳过：视为成功而非失败，
        # 调用方无需重试。duplicated=True 复用既有「未产生新通知」语义。
        if duplicated:
            return success_response(
                data=PublishNotifyResult(notify_id=None, duplicated=True)
            )
        return error_response(code=500, msg="发布系统通知失败：未返回通知记录")
    return success_response(
        data=PublishNotifyResult(notify_id=item.id, duplicated=duplicated)
    )


__all__ = ["rpc_publish_notify"]
