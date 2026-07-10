"""消息系统「推送」模块的 FastAPI HTTP 接口（挂载在 /api/v1/message 下）。

通过 FastStream 的 FastAPI 插件（lifespan 接入）暴露标准 REST 接口，
供 FastapiApp / RPA-Browser / 前端（经 nodejs-pptr 转发）调用，
替代「直接调用 PushMe / PushPlus 第三方接口」的方式。

接口命名、返回结构均参考项目内其它微服务（RPA-Browser）：
- 路由前缀 /api/v1/message/push
- 统一返回 StandardResponse{code, data, msg}

认证说明（微服务间完全互信）：
不做令牌 / JWT 校验。用户信息完全来自上游 nodejs-pptr 代理在转发请求时注入的
x-bili-* 请求头（网关侧已清除客户端伪造值并重写为可信登录态）。本接口直接信任
这些头：以 x-bili-mid 是否非空判断登录态。推送时根据用户信息进行标注（标题前缀），
并在请求声明 requires_login 时据此校验是否强制登录。
"""

from typing import Optional
from urllib.parse import unquote

from fastapi import APIRouter, Request
from pydantic import BaseModel

from app.broker import broker, message_exchange, message_queue
from app.consumer import _merge_config, format_user_label
from app.models import MessageUser, PushMessage
from app.services.push import PushMessageService

# 「推送」模块挂在消息系统路由 /api/v1/message 之下
router = APIRouter(prefix="/push", tags=["push"])


# 与 RPA-Browser / nodejs-pptr ProxyEndPort.setUserHeaders 注入的 x-bili-* 头一一对应
_HEADER_MAP = {
    "x-bili-mid": "mid",
    "x-bili-user-name": "user_name",
    "x-bili-uname": "uname",
    "x-bili-level": "level",
    "x-bili-role": "role",
    "x-bili-sign": "sign",
    "x-bili-sex": "sex",
    "x-bili-email": "email",
    "x-bili-vip-status": "vip_status",
    "x-bili-vip-type": "vip_type",
}


def build_user_from_headers(request: Request) -> Optional[MessageUser]:
    """从 x-bili-* 请求头还原推送发起方用户信息。

    上游代理（nodejs-pptr ProxyEndPort）会用 URL-encode 写入这些头，
    这里统一解码后构造 MessageUser；无相关头时返回 None（匿名推送）。
    """
    data: dict = {}
    for header, field in _HEADER_MAP.items():
        val = request.headers.get(header)
        if val:
            data[field] = unquote(val)
    if not data:
        return None
    return MessageUser(**data)


def is_logged_in(user: Optional[MessageUser]) -> bool:
    """依据 pptr 注入的 x-bili-mid 判断用户是否处于有效登录态。

    微服务间完全互信，登录态完全由网关重写后的 x-bili-mid 头决定：
    mid 非空即为已登录，空字符串表示未登录。
    """
    return bool(user and user.mid and str(user.mid).strip())


# 与 RPA-Browser 的 StandardResponse 保持同构（code / data / msg）
class StandardResponse(BaseModel):
    code: int = 0
    data: Optional[object] = None
    msg: str = "success"


class TestPushRequest(BaseModel):
    title: str = "测试推送"
    content: str = "这是一条来自 message-service 的测试推送"
    # pushme/pushplus 的模板类型，例如 text/markdown/html/json 等
    push_type: Optional[str] = "text"
    # 渠道配置；为空时使用 message-service 的全局环境变量配置
    config: Optional[dict] = None
    # 是否需要强制登录：为 True 且上游 pptr 未注入有效 x-bili-mid 时，拒绝推送并返回需登录提示
    requires_login: bool = False


class TestPushResponse(BaseModel):
    success: bool
    message: str
    # 本次成功推送所经过的渠道（简化：由 message-service 统一分发）
    sent_channels: list[str] = []


@router.post("/push", response_model=StandardResponse)
async def push_message(req: PushMessage, request: Request) -> StandardResponse:
    """投递一条推送请求到 RabbitMQ，由 message-service 消费者异步分发。

    等价于各微服务原先「直接调用 PushMe / PushPlus 接口」的逻辑，
    现在统一改为调用本接口，由 message-service 集中推送。
    上游透传的 x-bili-* 用户信息会被解析并随消息一并投递，供消费者写入推送内容。

    微服务间完全互信，不做令牌校验；登录态直接由 pptr 注入的 x-bili-mid 决定。
    当 req.requires_login 为 True 而用户未登录时，返回 code=401 并标注 require_login，
    告知调用方需要强制登录。
    """
    user = build_user_from_headers(request)
    # 强制登录校验：仅依据 pptr 注入的 x-bili-mid 判断，未登录则拒绝并提示
    if req.requires_login and not is_logged_in(user):
        return StandardResponse(
            code=401,
            msg="该推送需要登录后才能触发，请先登录",
            data={"require_login": True},
        )
    if user is not None:
        req.user = user
    try:
        await broker.publish(
            message=req.model_dump(),
            exchange=message_exchange,
            routing_key="message.push",
            queue=message_queue,
        )
    except Exception as e:  # noqa: BLE001
        return StandardResponse(code=500, msg=f"发布推送消息失败: {e}")
    return StandardResponse(data={"title": req.title})


@router.post("/test", response_model=StandardResponse)
async def test_push(req: TestPushRequest, request: Request) -> StandardResponse:
    """立即发送一条测试推送（不经过队列），便于前端 / 用户验证渠道配置。"""
    user = build_user_from_headers(request)
    # 强制登录校验：仅依据 pptr 注入的 x-bili-mid 判断，未登录则拒绝并提示
    if req.requires_login and not is_logged_in(user):
        return StandardResponse(
            code=401,
            msg="该推送需要登录后才能触发，请先登录",
            data={"require_login": True},
        )
    # 复用 consumer 的合并逻辑：消息内 config 优先，否则回落全局环境变量配置
    merged = _merge_config(
        PushMessage(
            title=req.title,
            content=req.content,
            push_type=req.push_type,
            config=req.config,
        )
    )
    service = PushMessageService(merged, push_type=req.push_type)
    # 将上游透传的用户信息写入推送标题，便于区分推送来源
    title = req.title
    label = format_user_label(user)
    if label:
        title = f"[{label}] {title}"
    try:
        sent = await service.send(title, req.content)
        if not sent:
            # send 返回 False 表示没有任何已启用的推送渠道（未配置或全局/用户配置为空）
            return StandardResponse(
                code=400,
                msg="未找到可用的推送渠道，请检查通知配置（MESSAGE_CONFIG 或传入的 config）",
                data=TestPushResponse(
                    success=False,
                    message="无可用推送渠道",
                ),
            )
        return StandardResponse(
            data=TestPushResponse(
                success=True,
                message="测试推送已发送，请检查对应渠道是否收到",
            )
        )
    except Exception as e:  # noqa: BLE001
        return StandardResponse(
            code=500,
            msg=f"测试推送失败: {e}",
            data=TestPushResponse(success=False, message=str(e)),
        )
