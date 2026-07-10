from typing import Optional

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


class MessageUser(BaseModel):
    """推送发起方的用户信息。

    由上游 FastapiApp / RPA-Browser 经 nodejs-pptr 代理通过 x-bili-* 请求头透传，
    message-service 解析后写入推送内容（标题前缀），便于区分「是谁触发的推送」。
    字段与 RPA-Browser / nodejs-pptr ProxyEndPort 中 setUserHeaders 注入的
    x-bili-* 头一一对应。
    """

    model_config = ConfigDict(extra="ignore")

    # 用户唯一 ID（B 站 mid）
    mid: Optional[str] = None
    # 登录用户名
    user_name: Optional[str] = None
    # 用户昵称（uname）
    uname: Optional[str] = None
    # 用户等级
    level: Optional[str] = None
    # 角色
    role: Optional[str] = None
    # 个性签名
    sign: Optional[str] = None
    # 性别
    sex: Optional[str] = None
    # 邮箱
    email: Optional[str] = None
    # 大会员状态
    vip_status: Optional[str] = None
    # 大会员类型
    vip_type: Optional[str] = None


class PushMessage(BaseModel):
    """通过消息队列投递的推送请求（消息系统的「推送」模块）。

    等价于各微服务原先「直接调用 PushMe / PushPlus 接口」的逻辑，
    现在统一改为投递到 message-service，由消费者异步分发。
    """

    title: str
    content: str
    # pushme/pushplus 的模板类型，例如 text/markdown/html/json 等
    push_type: Optional[str] = "text"
    # 渠道配置；为空时回落到 message-service 的全局环境变量配置
    config: Optional[PushChannelConfig] = None
    # 推送发起方用户信息（上游经 x-bili-* 头透传），发送时写入推送内容
    user: Optional[MessageUser] = None
    # 是否需要强制登录：为 True 且上游 pptr 未注入有效 x-bili-mid 时，拒绝推送并返回需登录提示
    requires_login: bool = False


class TestPushRequest(BaseModel):
    """「推送」模块的测试推送请求。"""

    title: str = "测试推送"
    content: str = "这是一条来自 message-service 的测试推送"
    # pushme/pushplus 的模板类型，例如 text/markdown/html/json 等
    push_type: Optional[str] = "text"
    # 渠道配置；为空时使用 message-service 的全局环境变量配置
    config: Optional[dict] = None
    # 是否需要强制登录：为 True 且上游 pptr 未注入有效 x-bili-mid 时，拒绝推送并返回需登录提示
    requires_login: bool = False


class TestPushResponse(BaseModel):
    """「推送」模块的测试推送响应。"""

    success: bool
    message: str
    # 本次成功推送所经过的渠道（简化：由 message-service 统一分发）
    sent_channels: list[str] = []
