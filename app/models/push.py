"""「推送」模块的数据模型。

渠道配置（PushChannelConfig）与队列载体（PushMessagePayload）统一来自公共库
`bili_common.models.push`（SQLModel），RPA-Browser 与 be-message-service 共用同一份
契约，避免两端漂移。

本文件仅保留「推送」模块自身的 HTTP 请求/响应体（PushMessage / TestPushRequest /
TestPushResponse / FeedbackRequest），与队列载体解耦。
"""



from sqlmodel import SQLModel, Field

from bili_common.models.push import PushChannelConfig, PushMessagePayload


class PushMessage(SQLModel):
    """「推送」模块的 HTTP 请求体（POST /api/v1/message/push）。

    由 FastapiApp / RPA-Browser / 前端（经 nodejs-pptr 转发）调用本接口时传入，
    等价于各微服务原先「直接调用 PushMe / PushPlus 接口」的逻辑。api 层收到后会
    转换为 PushMessagePayload 投递到 RabbitMQ，由消费者异步分发。
    """

    title: str
    content: str
    # pushme/pushplus 的模板类型，例如 text/markdown/html/json 等
    push_type: str | None = "text"
    # 渠道配置；为空时回落到 message-service 的全局环境变量配置
    config: PushChannelConfig | None = None


class TestPushRequest(SQLModel):
    """「推送」模块的测试推送请求。"""

    title: str = "测试推送"
    content: str = "这是一条来自 message-service 的测试推送"
    # pushme/pushplus 的模板类型，例如 text/markdown/html/json 等
    push_type: str | None = "text"
    # 渠道配置；为空时使用 message-service 的全局环境变量配置
    config: dict | None = None


class TestPushResponse(SQLModel):
    """「推送」模块的测试推送响应。"""

    success: bool
    message: str
    # 本次成功推送所经过的渠道（简化：由 message-service 统一分发）
    sent_channels: list[str] = Field(default_factory=list)


class FeedbackRequest(SQLModel):
    """前端反馈请求体（POST /api/v1/message/push/feedback）。

    source 标注反馈来源页面 / 模块（如「首页」「抽奖数据页」），
    用于后端拼接推送标题前缀，告诉站长这条反馈来自哪里；
    content 为反馈正文，contact 为可选联系方式。
    """

    content: str
    contact: str | None = None
    source: str | None = None


# 重新导出公共载体，方便其他模块直接 `from app.models.push import PushMessagePayload`
__all__ = [
    "PushChannelConfig",
    "PushMessagePayload",
    "PushMessage",
    "TestPushRequest",
    "TestPushResponse",
    "FeedbackRequest",
]
