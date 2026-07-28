from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.models import PushChannelConfig


class Settings(BaseSettings):
    """message-service 运行时配置。

    推送渠道分两类：
    1. 消息内携带的 config（来自 RPA-Browser 的 per-user 配置）——优先级最高。
    2. 环境变量 MESSAGE_CONFIG（JSON）中的全局渠道配置——FastapiApp / RPA-Browser
       通过同一个环境变量共用这份内容，作为全局兜底。

    MESSAGE_CONFIG 示例（任意 PushChannelConfig 字段均可放入）：
      MESSAGE_CONFIG='{"pushme_key":"Uxxx","push_plus_token":"yyy",
        "smtp_server":"smtp.x","smtp_ssl":"true","smtp_email":"a@b.c",
        "smtp_password":"pw","smtp_name":"告警"}'
    """

    # RabbitMQ
    rabbitmq_url: str = "amqp://guest:guest@rabbitmq:5672/?heartbeat=180"

    # HTTP 健康检查服务监听地址（AsgiFastStream 暴露 /health 路由）
    http_host: str = "0.0.0.0"
    http_port: int = 18739

    # 推送渠道默认端点（可被 MESSAGE_CONFIG 中的 pushme_url / push_plus_url 覆盖）
    pushme_url: str = "https://push.i-i.me"
    pushplus_url: str = "http://www.pushplus.plus/send"

    # 全局渠道配置：单个 JSON 环境变量，与 fastapiapp / rpa-browser 共用同一份
    # 类型为 pydantic PushChannelConfig，由 pydantic-settings 自动解析 JSON，无需 Json() 包装
    message_config: PushChannelConfig = PushChannelConfig(hitokoto=False)

    model_config = SettingsConfigDict(
        env_file=("app/.env",),
        case_sensitive=False,
        extra="ignore",
    )


settings = Settings()

print(f"环境变量设置:{settings}")
