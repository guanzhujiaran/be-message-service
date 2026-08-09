from pydantic_settings import BaseSettings, SettingsConfigDict

from app.models.push import PushChannelConfig


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

    # ==================== RabbitMQ ====================
    rabbitmq_url: str = "amqp://guest:guest@rabbitmq:5672/?heartbeat=180"

    # ==================== HTTP ====================
    # HTTP 健康检查服务监听地址（AsgiFastStream 暴露 /health 路由）
    http_host: str = "0.0.0.0"
    http_port: int = 18739

    # ==================== MySQL ====================
    # 主库（元数据库）连接串：系统通知 / 事件提醒 / 私信索引 / 会话 / 设置等表所在库。
    # 私信「内容」不落在这里，而是按月分库 + 库内分表 100 张（见 app.core.sharding）。
    mysql_message_url: str = (
        "mysql+aiomysql://root:root@mysql:3306/BiliMessageDB?charset=utf8mb4"
    )
    # 私信内容分库名前缀：实际库名为 {prefix}_{YYYYMM}，如 bili_msg_content_202608
    dm_content_db_prefix: str = "bili_msg_content"
    # 私信内容分表名前缀：实际表名为 {prefix}_{00..99}
    dm_content_table_prefix: str = "msg_content"
    # 单个月度库内的分表数量（msgkey 取余路由）
    dm_content_table_count: int = 100

    # 连接池（设备较小，池子开小一点）
    mysql_pool_size: int = 10
    mysql_max_overflow: int = 15
    mysql_pool_recycle: int = 300
    mysql_echo: bool = False

    # ==================== pptr Postgres（由 be-message 接管）====================
    # 直连 be-gateway 的 Postgres（库 PPTR_Bili_Lot）。
    # 历史：早期本服务仅只读地取回评论 / 私信作者的展示信息（昵称 / 头像 / 等级 /
    #       大会员 / 性别 / 签名）与 @ 搜索；现 be-message **彻底接管**该库（可读可写），
    #       以当前库结构为 baseline，由独立 Alembic 分支（alembic_pptr/）管理其版本演进。
    postgres_pptr_url: str = (
        "postgresql+asyncpg://postgres:postgres@postgres:5432/PPTR_Bili_Lot"
    )
    # pptr 用户表所在 schema（sequelize 默认 public）
    postgres_pptr_schema: str = "public"
    # 只读连接池（读多写零，池子略小）
    postgres_pptr_pool_size: int = 5
    postgres_pptr_max_overflow: int = 10
    postgres_pptr_pool_recycle: int = 300
    postgres_pptr_echo: bool = False

    # ==================== Alembic ====================
    # 应用启动时自动执行 alembic upgrade head
    alembic_auto_migrate: bool = True
    alembic_upgrade_target: str = "head"

    # ==================== msgkey（雪花 ID）====================
    # msgkey 起始纪元（毫秒时间戳）：2024-01-01 00:00:00 UTC+8
    msgkey_epoch_ms: int = 1704038400000
    # 本实例 worker 编号（多实例部署时必须互不相同，0~1023）
    msgkey_worker_id: int = 1

    # ==================== uid（短雪花 ID，分钟步进）====================
    # uid epoch（秒级时间戳）：默认 2026-08-08 00:00:00 UTC+8，可通过环境变量 UID_EPOCH_SEC 覆盖
    uid_epoch_sec: int = 1756684800
    # uid worker 编号（0~15），通过环境变量 UID_WORKER_ID 设置，多实例部署时互不相同
    uid_worker_id: int = 1

    # ==================== 活跃度 ====================
    # 用户在该秒数内有过行为即视为「活跃用户」（用于前端轮询节奏判定，与消息送达无关）
    active_user_window_seconds: int = 300
    # 定时标记已发布的系统通知为「已投递」的任务间隔（秒）
    notify_dispatch_interval_seconds: int = 60
    # 是否启用后台定时任务
    scheduler_enabled: bool = True

    # ==================== 私信策略 ====================
    # 消息可撤回的时间窗口（秒），超过则不允许撤回
    dm_recall_window_seconds: int = 120
    # 会话列表 / 消息列表默认页大小
    dm_default_page_size: int = 20
    # 私信内容异步落库失败时是否降级为同步写入
    dm_content_sync_fallback: bool = True

    # ==================== 评论系统 ====================
    # 单条评论最多携带的图片数（对齐 B 站九宫格）
    comment_picture_max: int = 9
    # 图片URL域名白名单；**留空表示不限制**。
    # 只存 URL、不做本地转存，因此白名单是防盗链 / 防垃圾外链的唯一手段。
    # 环境变量以 JSON 数组传入，如 COMMENT_PICTURE_DOMAINS='["i0.hdslb.com"]'
    comment_picture_domains: list[str] = []
    # 单条评论最多 @ 的人数
    comment_at_max: int = 10
    # 评论正文最大长度（与原 Node 端 TComment.content 的 4096 保持一致）
    comment_message_max_length: int = 4096
    # 评论列表默认页大小
    comment_default_page_size: int = 20
    # 一级评论下内嵌展示的楼中楼预览条数，超出需点击「查看更多」
    comment_sub_preview_count: int = 3
    # 评论发布模式：是否「先审后发」。
    # - True（默认） ：所有原本会直接展示(NORMAL)的评论一律先进入审核态(AUDITING)，
    #   对外不可见，需管理端审核通过后（置 NORMAL）才展示；命中高危词仍直接驳回。
    # - False        ：命中高危词直接驳回(REJECTED)、命中疑似词进审核(AUDITING)，
    #   其余评论直接对外展示(NORMAL)。
    comment_pre_audit: bool = True
    # 私信发布模式：是否「先审后发」（默认关闭，与评论默认值相反）。
    # - False（默认）：私信发布即直接对接收方可见(NORMAL)。
    # - True         ：新私信先进入审核态(AUDITING)，对接收方不可见，
    #   需管理端审核通过后（置 NORMAL）才对接收方可见；发送者本人始终可见。
    dm_pre_audit: bool = False

    # ==================== pptr 用户成长等级 ====================
    # 经验 / 等级算法配置（原在 pptr 侧 common_config.level_config，
    # 现已整体下沉到 be-message RPC，由 Python 侧统一计算）。
    # 各等级升级所需的「累积经验值」（到级阈值）。
    level_max_level: int = 6
    level_daily_exp_bonus: int = 3  # 每日首次登录奖励经验值
    level_exp_requirements: dict[int, int] = {
        1: 1000,
        2: 5000,
        3: 20000,
        4: 80000,
        5: 288000,
        6: 999999999,
    }
    # 各角色（等级 / 管理员）的展示文案（对齐 pptr `user_role_const` 的 getRoleDescription）。
    # 环境变量以 JSON 对象传入，如：
    #   PPTR_LEVEL_ROLE_DESCRIPTION='{"level0":"普通用户 (Lv0)","root":"系统管理员"}'
    level_role_description: dict[str, str] = {
        "level0": "普通用户 (Lv0)",
        "level1": "普通用户 (Lv1)",
        "level2": "普通用户 (Lv2)",
        "level3": "普通用户 (Lv3)",
        "level4": "普通用户 (Lv4)",
        "level5": "普通用户 (Lv5)",
        "level6": "普通用户 (Lv6)",
        "root": "系统管理员",
    }

    # ==================== JWT（用户网关下沉 be-message）====================
    # 与 pptr 侧 JwtModule.js 的 secretKey 保持一致
    jwt_secret: str = "关注永雏塔菲喵，关注永雏塔菲谢谢喵！114514"
    jwt_algorithm: str = "HS256"
    jwt_expires_seconds: int = 15 * 24 * 3600  # 15 天

    # ==================== 前端地址（Casdoor 回调重定向）====================
    frontend_url: str = ""

    # ==================== Casdoor（用户网关下沉 be-message）====================
    casdoor_endpoint: str = ""
    casdoor_client_id: str = ""
    casdoor_client_secret: str = ""
    casdoor_organization: str = ""
    casdoor_application: str = ""
    casdoor_service: str = ""
    casdoor_certificate: str = ""
    casdoor_enabled: bool = False

    # ==================== 推送渠道 ====================
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

    def model_post_init(self, __context) -> None:
        """.env 中证书的换行符是字面 \n，需转为真实换行符才能被 PEM 解析。"""
        if self.casdoor_certificate and "\\n" in self.casdoor_certificate:
            self.casdoor_certificate = self.casdoor_certificate.replace("\\n", "\n")

    @property
    def mysql_sync_url(self) -> str:
        """同步驱动版连接串（Alembic offline / Schema 校验等场景使用）。"""
        return self.mysql_message_url.replace("mysql+aiomysql://", "mysql+pymysql://")

    @property
    def postgres_pptr_sync_url(self) -> str:
        """pptr Postgres 同步驱动版连接串（Alembic_pptr offline / 接管迁移场景使用）。"""
        return self.postgres_pptr_url.replace(
            "postgresql+asyncpg://", "postgresql+psycopg2://"
        )


settings = Settings()
