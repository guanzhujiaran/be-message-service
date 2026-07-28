"""「推送」模块的数据模型。

包含：渠道配置（PushChannelConfig）、HTTP 请求/响应体（PushMessage、
TestPushRequest、TestPushResponse）、消息队列专用载体（PushMessagePayload）。
"""

from pydantic import BaseModel, ConfigDict


class PushChannelConfig(BaseModel):
    """推送渠道配置模型（消息系统「推送」模块的渠道配置）。

    字段与 RPA-Browser / FastapiApp 的 PushChannelConfig 保持一致，以便直接接收
    它们序列化后的 per-user 配置。未知字段一律忽略。
    """

    model_config = ConfigDict(extra="ignore")

    # 一言（随机句子）
    hitokoto: bool = True

    # Bark
    bark_push: str = ""
    bark_archive: str = ""
    bark_group: str = ""
    bark_sound: str = ""
    bark_icon: str = ""
    bark_level: str = ""
    bark_url: str = ""

    # 钉钉机器人
    dd_bot_secret: str = ""
    dd_bot_token: str = ""

    # 飞书机器人
    fskey: str = ""

    # go-cqhttp
    gobot_url: str = ""
    gobot_qq: str = ""
    gobot_token: str = ""

    # Gotify
    gotify_url: str = ""
    gotify_token: str = ""
    gotify_priority: int = 0

    # iGot
    igot_push_key: str = ""

    # Server 酱
    push_key: str = ""

    # PushDeer
    deer_key: str = ""
    deer_url: str = ""

    # Synology Chat
    chat_url: str = ""
    chat_token: str = ""

    # PushPlus
    push_plus_token: str = ""
    push_plus_url: str = ""
    push_plus_user: str = ""
    push_plus_template: str = "html"
    push_plus_channel: str = "wechat"
    push_plus_webhook: str = ""
    push_plus_callbackurl: str = ""
    push_plus_to: str = ""

    # 微加机器人
    we_plus_bot_token: str = ""
    we_plus_bot_receiver: str = ""
    we_plus_bot_version: str = "pro"

    # Qmsg 酱
    qmsg_key: str = ""
    qmsg_type: str = ""

    # 企业微信
    qywx_origin: str = ""
    qywx_am: str = ""
    qywx_key: str = ""

    # Telegram
    tg_bot_token: str = ""
    tg_user_id: str = ""
    tg_api_host: str = ""
    tg_proxy_auth: str = ""
    tg_proxy_host: str = ""
    tg_proxy_port: str = ""

    # 智能微秘书
    aibotk_key: str = ""
    aibotk_type: str = ""
    aibotk_name: str = ""

    # SMTP 邮件
    smtp_server: str = ""
    smtp_ssl: str = "false"
    smtp_email: str = ""
    smtp_password: str = ""
    smtp_name: str = ""

    # PushMe
    pushme_key: str = ""
    pushme_url: str = ""

    # Chronocat
    chronocat_qq: str = ""
    chronocat_token: str = ""
    chronocat_url: str = ""

    # 自定义 Webhook
    webhook_url: str = ""
    webhook_body: str = ""
    webhook_headers: str = ""
    webhook_method: str = ""
    webhook_content_type: str = ""

    # Ntfy
    ntfy_url: str = ""
    ntfy_topic: str = ""
    ntfy_priority: str = "3"
    ntfy_token: str = ""
    ntfy_username: str = ""
    ntfy_password: str = ""
    ntfy_actions: str = ""

    # WxPusher
    wxpusher_app_token: str = ""
    wxpusher_topic_ids: str = ""
    wxpusher_uids: str = ""


class PushMessage(BaseModel):
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


class PushMessagePayload(BaseModel):
    """消息队列（RabbitMQ）中「推送」消息的专用载体。

    仅用于 broker 投递与 @broker.subscriber 消费，与对外的 HTTP 请求体
    PushMessage 解耦：请求体描述「调用方想推送什么」，本模型描述「队列里实际
    流转的推送消息」，两者可各自独立演进（如本模型后续可扩展 trace_id、
    published_at 等消息投递元数据，而不影响 HTTP 契约）。
    """

    title: str
    content: str
    # pushme/pushplus 的模板类型，例如 text/markdown/html/json 等
    push_type: str | None = "text"
    # 渠道配置；为空时回落到 message-service 的全局环境变量配置
    config: PushChannelConfig | None = None


class TestPushRequest(BaseModel):
    """「推送」模块的测试推送请求。"""

    title: str = "测试推送"
    content: str = "这是一条来自 message-service 的测试推送"
    # pushme/pushplus 的模板类型，例如 text/markdown/html/json 等
    push_type: str | None = "text"
    # 渠道配置；为空时使用 message-service 的全局环境变量配置
    config: dict | None = None


class TestPushResponse(BaseModel):
    """「推送」模块的测试推送响应。"""

    success: bool
    message: str
    # 本次成功推送所经过的渠道（简化：由 message-service 统一分发）
    sent_channels: list[str] = []


class FeedbackRequest(BaseModel):
    """前端反馈请求体（POST /api/v1/message/push/feedback）。

    source 标注反馈来源页面 / 模块（如「首页」「抽奖数据页」），
    用于后端拼接推送标题前缀，告诉站长这条反馈来自哪里；
    content 为反馈正文，contact 为可选联系方式。
    """

    content: str
    contact: str | None = None
    source: str | None = None
