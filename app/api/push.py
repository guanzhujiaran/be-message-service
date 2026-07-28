"""消息系统「推送」模块的 FastAPI HTTP 接口（挂载在 /api/v1/message 下）。

通过 FastStream 的 FastAPI 插件（lifespan 接入）暴露标准 REST 接口，
供 FastapiApp / RPA-Browser / 前端（经 nodejs-pptr 转发）调用，
替代「直接调用 PushMe / PushPlus 第三方接口」的方式。

接口命名、返回结构均参考项目内其它微服务（RPA-Browser）：
- 路由前缀 /api/v1/message/push
- 统一返回 StandardResponse{code, data, msg}

认证说明（微服务间完全互信）：
不做令牌 / JWT 校验。用户信息由 app.dependencies.user 中的 FastAPI 依赖
从上游 nodejs-pptr 注入的 x-bili-* 请求头解析（网关侧已清除客户端伪造值并
重写为可信登录态）。推送时根据用户信息进行标注（标题前缀）。
"""

from fastapi import APIRouter

from app.core.broker import broker, message_exchange, message_queue
from app.dependencies import CurrentUser
from app.models import (
    FeedbackRequest,
    PushMessage,
    PushMessagePayload,
    StandardResponse,
    TestPushRequest,
    TestPushResponse,
)
from app.services.push import PushMessageService
from app.services.push_helper import format_user_label, merge_config

# 「推送」模块挂在消息系统路由 /api/v1/message 之下
router = APIRouter(prefix="/api/v1/message/push", tags=["push"])


@router.post("/push", response_model=StandardResponse)
async def push_message(req: PushMessage, user: CurrentUser) -> StandardResponse:
    """投递一条推送请求到 RabbitMQ，由 message-service 消费者异步分发。

    等价于各微服务原先「直接调用 PushMe / PushPlus 接口」的逻辑，
    现在统一改为调用本接口，由 message-service 集中推送。
    上游透传的 x-bili-* 用户信息由 CurrentUser 依赖解析，
    在投递前直接拼进推送标题（标题前缀），便于消费者区分推送来源。

    微服务间完全互信，不做令牌校验。
    """
    # 将上游透传的用户信息写入推送标题，便于区分推送来源
    label = format_user_label(user)
    title = f"[{label}] {req.title}" if label else req.title
    # 转换为消息队列专用载体后再投递，HTTP 请求体与 MQ 消息模型解耦
    payload = PushMessagePayload(
        title=title,
        content=req.content,
        push_type=req.push_type,
        config=req.config,
    )
    try:
        await broker.publish(
            message=payload.model_dump(),
            exchange=message_exchange,
            routing_key="message.push",
            queue=message_queue,
        )
    except Exception as e:  # noqa: BLE001
        return StandardResponse(code=500, msg=f"发布推送消息失败: {e}")
    return StandardResponse(data={"title": title})


@router.post("/test", response_model=StandardResponse)
async def test_push(req: TestPushRequest, user: CurrentUser) -> StandardResponse:
    """立即发送一条测试推送（不经过队列），便于前端 / 用户验证渠道配置。"""
    # 复用 push_helper 的合并逻辑：消息内 config 优先，否则回落全局环境变量配置
    merged = merge_config(
        PushMessagePayload(
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


@router.post("/feedback", response_model=StandardResponse)
async def submit_feedback(req: FeedbackRequest, user: CurrentUser) -> StandardResponse:
    """前端提交反馈，仅推送到站长自己的推送设置（全局 message_config）。

    不接收也不使用调用方传入的 per-user 渠道配置，固定回落到 message-service 的
    全局环境变量配置（MESSAGE_CONFIG），确保反馈只发到站长本人。
    标题包含来源（source）与用户标签，便于区分反馈来自哪个页面 / 哪位用户。

    微服务间完全互信，不做令牌校验。
    """
    if not req.content or not req.content.strip():
        return StandardResponse(code=400, msg="反馈内容不能为空")

    label = format_user_label(user)
    source = req.source.strip() if req.source else ""
    prefix = f"用户反馈|{source}" if source else "用户反馈"
    title = f"[{prefix}] {label}" if label else f"[{prefix}] 匿名用户"

    content = req.content.strip()
    if req.contact and req.contact.strip():
        content += f"\n\n联系方式: {req.contact.strip()}"

    # 反馈只发到站长自己的推送设置：config=None 回落全局 message_config
    merged = merge_config(
        PushMessagePayload(
            title=title,
            content=content,
            push_type="text",
            config=None,
        )
    )
    service = PushMessageService(merged, push_type="text")
    try:
        sent = await service.send(title, content)
        if not sent:
            return StandardResponse(
                code=400,
                msg="站长未配置任何推送渠道，反馈无法送达",
                data=TestPushResponse(success=False, message="无可用推送渠道"),
            )
        return StandardResponse(
            data=TestPushResponse(success=True, message="反馈已提交给站长"),
        )
    except Exception as e:  # noqa: BLE001
        return StandardResponse(
            code=500,
            msg=f"提交反馈失败: {e}",
            data=TestPushResponse(success=False, message=str(e)),
        )
