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
    # FastStream（RabbitMQ 消费者 / 发布者）内部日志级别：DEBUG / INFO / WARNING / ERROR / CRITICAL，
    # 通过环境变量 FASTSTREAM_LOG_LEVEL 覆盖。仅影响 FastStream 框架自身的标准库日志，
    # 不影响本项目 loguru 业务日志。
    faststream_log_level: str = "INFO"
    # 业务日志（loguru）输出级别：DEBUG / INFO / WARNING / ERROR / CRITICAL，通过环境变量
    # LOG_LEVEL 覆盖。约定：**生产只打印 WARNING 及以上**（默认 WARNING，压日志量），
    # 开发环境设 DEBUG 看全量。日志默认只输出到 stderr（容器日志由 docker logs 收集）。
    log_level: str = "WARNING"
    # 运行环境：development / production，通过环境变量 APP_ENV 覆盖（默认 production）。
    # 仅 development 会在 LOG_FILE_DIR 下额外把 WARNING 及以上日志写入文件，便于排查；
    # production 不写任何日志文件。
    app_env: str = "production"
    # 开发环境日志文件目录（环境变量 LOG_FILE_DIR 覆盖，默认项目下 logs/）
    log_file_dir: str = "logs"

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
    # 只读连接池（读多写零；seed 高并发审核/Feed 渲染抢连接，2.46.0 调大 5+10→10+20）
    postgres_pptr_pool_size: int = 10
    postgres_pptr_max_overflow: int = 20
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
    # uid 序列号位宽（默认 4 = 每 worker 每分钟最多 16 个，总位数恒 39 bits）。
    # 开发/测试环境可放宽（如 7 = 每分钟 128 个）支撑灌数；⚠️ 变更位宽会改变对外
    # ID 数值空间、与已发布 ID 可能重叠，仅限清库重建的环境启用，生产保持默认 4。
    uid_sequence_bits: int = 4

    # ==================== moment_id（动态 ID，独立的短雪花 ID）====================
    # 与 uid 使用**不同的** epoch / worker 配置，避免两者在同一分钟内序列号碰撞
    # （uid 与 moment_id 数值空间隔离，可安全共用 BIGINT 主键列且无重复风险）。
    # moment_id epoch（秒级时间戳）：默认 2026-08-09 00:00:00 UTC+8，可通过环境变量 MOMENT_ID_EPOCH_SEC 覆盖
    moment_id_epoch_sec: int = 1756771200
    # moment_id worker 编号（0~15），通过环境变量 MOMENT_ID_WORKER_ID 设置，多实例部署时互不相同
    moment_id_worker_id: int = 2
    # moment_id 序列号位宽（默认 4 = 每 worker 每分钟最多 16 个，总位数恒 39 bits）。
    # 开发/测试环境可放宽（如 7 = 每分钟 128 个）支撑灌数；⚠️ 变更位宽会改变对外
    # ID 数值空间、与已发布 ID 可能重叠，仅限清库重建的环境启用，生产保持默认 4。
    moment_id_sequence_bits: int = 4
    # topic_id epoch（秒级时间戳）：默认 2026-08-16 00:00:00 UTC+8，可通过环境变量 TOPIC_ID_EPOCH_SEC 覆盖
    topic_id_epoch_sec: int = 1756915200
    # topic_id worker 编号（0~15），通过环境变量 TOPIC_ID_WORKER_ID 设置，多实例部署时互不相同
    topic_id_worker_id: int = 3
    # topic_id 序列号位宽（默认 4 = 每 worker 每分钟最多 16 个，总位数恒 39 bits）。
    # 开发/测试环境可放宽（如 7 = 每分钟 128 个）支撑灌数；⚠️ 变更位宽会改变对外
    # ID 数值空间、与已发布 ID 可能重叠，仅限清库重建的环境启用，生产保持默认 4。
    topic_id_sequence_bits: int = 4

    # ==================== GeoIP（IP 属地解析）====================
    # GeoLite2 mmdb 数据库目录（含 GeoLite2-City.mmdb 等）。
    # - 本地开发：默认 ./mmdb（相对 be-message-service 工作目录），用
    #   scripts/download_geoip_mmdb.py 下载；
    # - Docker：挂载 docker_vol/geoip/mmdb 到容器内 /app/mmdb（见 docker-compose.yml），
    #   与本地数据相互独立，各管各的。
    geoip_mmdb_dir: str = "./mmdb"

    # ==================== 活跃度 ====================
    # 用户在该秒数内有过行为即视为「活跃用户」（用于前端轮询节奏判定，与消息送达无关）
    active_user_window_seconds: int = 300
    # 定时标记已发布的系统通知为「已投递」的任务间隔（秒）
    notify_dispatch_interval_seconds: int = 60
    # 是否启用后台定时任务
    scheduler_enabled: bool = True

    # ==================== EdgeRank 推荐排序（2.27.0）====================
    # 公域 Feed（综合 Feed / 话题 Feed / 话题广场）的推荐排序算法：
    #   score = (w_like·likeCount + w_comment·commentCount + w_repost·repostCount
    #            + w_view·viewCount + w_favorite·favoriteCount) × decay(age)
    #   decay(age) = 0.5 ** (age_seconds / half_life_seconds)
    # 权重以 JSON 对象传入（环境变量），如
    #   EDGERANK_FEED_WEIGHTS='{"like":1.0,"comment":1.5,"repost":2.0,"view":0.1,"favorite":1.2}'
    # 综合 Feed 半衰期（秒）：内容保鲜期较长，默认 24h
    edgerank_feed_half_life_seconds: int = 86400
    edgerank_feed_weights: dict[str, float] = {
        "like": 1.0,
        "comment": 1.5,
        "repost": 2.0,
        "view": 0.1,
        "favorite": 1.2,
        "share": 0.8,
    }
    # 话题 Feed（/feed/topic/{id}?sort=hot）半衰期（秒）：话题热点时效性强，默认 6h，
    # 评论/转发权重更高（话题讨论氛围）
    edgerank_topic_feed_half_life_seconds: int = 21600
    edgerank_topic_feed_weights: dict[str, float] = {
        "like": 1.2,
        "comment": 1.8,
        "repost": 1.5,
        "view": 0.05,
        "favorite": 1.0,
    }
    # 话题广场 / 热搜半衰期（秒）：默认 12h；话题分 = Σ(w·log(count+1))·decay(pubTime)
    edgerank_topic_square_half_life_seconds: int = 43200
    edgerank_topic_square_weights: dict[str, float] = {
        "dynCount": 1.0,
        "viewCount": 0.6,
        "isHot": 5.0,
        "sortWeight": 2.0,
    }
    # 综合 Feed recommend 模式候选集：最近 N 小时内、上限 M 条（走 idx_dynamic_pubtime_visible）
    edgerank_candidate_window_hours: int = 72
    edgerank_candidate_limit: int = 300
    # 总开关：False 时 recommend/hot 回退为时间倒序（打分函数返回时间等价分）
    edgerank_enabled: bool = True
    # ==================== EdgeRank 个性化（2.33.0）====================
    # 综合页 recommend 对登录用户叠加个性化因子：score = base + Σ(w_personal·signal)。
    # 三类信号（均为「加分」，不乘 decay，保证关注作者/偏好话题的新内容稳定靠前）：
    #   w_follow          关注作者（msg_user_follow，最强）
    #   w_liked_author    点赞过的作者（TMomentLike+TMoment，关注冷启动补充）
    #   w_topic           互动过的话题（TMomentTopicRel）
    # 未登录 / 开关关闭 → 退化为纯全局排序（与 2.32.0 一致）。
    edgerank_personalized_enabled: bool = True
    edgerank_personalized_follow_weight: float = 3.0
    edgerank_personalized_liked_author_weight: float = 1.5
    edgerank_personalized_topic_weight: float = 1.2
    # 点赞历史回看条数（信号来源上限，防全量扫描；0 表示不加载点赞/话题信号）
    edgerank_personalized_like_history_limit: int = 200
    # ==================== EdgeRank 匿名随机（2.34.0）====================
    # 未登录用户不以全局排序返回：以客户端 uniq_id 为随机种子派生一组扰动权重
    # （每项 × [1±perturb_ratio]），不同匿名用户/会话看到不同排序；
    # uniq_id 缺失时每次请求随机。
    edgerank_anon_randomize_enabled: bool = True
    edgerank_anon_perturb_ratio: float = 0.3
    # ==================== EdgeRank 多维打分（2.35.0）====================
    # content_quality 附加维度：
    edgerank_engagement_weight: float = 0.5  # 互动率 (like+comment+repost)/max(view,1)，防僵尸爆款
    edgerank_rich_weight: float = 0.4  # 内容丰富度（contentJson 含图片/视频/LINK 节点）
    edgerank_forward_penalty: float = -0.6  # FORWARD 转发惩罚（负权重）
    # fresh_bonus = w/(1+viewCount)：新内容冷启动，防被高互动旧内容埋没
    edgerank_fresh_weight: float = 1.0
    # feedback：点踩降权 = -w·dislike_ratio（dislike/(dislike+like)）
    edgerank_dislike_penalty: float = 2.0
    # 登录用户 last_clicklist 已互动作者/话题加权
    edgerank_click_weight: float = 1.0
    # author_signal：作者质量（moment_author_quality.avgEngagement）+ 刷屏惩罚
    edgerank_author_quality_weight: float = 0.8
    # 近 7 天发布量每超过该阈值 1 条惩罚分
    edgerank_author_publish_threshold: int = 5
    edgerank_author_spam_penalty: float = 0.2
    # 2.37.0 通用维度：举报数降权（每 1 条 pending 举报扣分，按 resourceType+bizId 统计）
    edgerank_report_penalty: float = 0.5
    # 作者粉丝/等级（moment_author_quality.fansCount / currentLevel，定时任务聚合）
    edgerank_fans_weight: float = 0.2  # log(1+fans)
    edgerank_level_weight: float = 0.1  # currentLevel
    # 2.43.0：时间衰减基准改用「最近活跃时间」（max(pubTime, 最后评论时间)），
    # 最后评论时间取评论系统 CommentSubject.updated_at；关闭则仅用发布时间
    edgerank_decay_use_last_activity: bool = True
    # ==================== 候选集多路召回（2.46.0）====================
    # sort=recommend 候选由「72h 最新 N 条」升级为五路召回并集去重；每路独立开关与上限。
    # 并集后仍受 edgerank_candidate_limit 总上限约束，召回阶段不排序（统一交 rank_feed 精排）。
    edgerank_recall_hot_enabled: bool = True  # 热门/趋势路：时间窗口最新 + 互动 top 兜底
    edgerank_recall_hot_limit: int = 200  # 时间窗口最新条数
    edgerank_recall_hot_top_limit: int = 100  # 互动量(like+comment+repost) top 兜底条数
    edgerank_recall_social_enabled: bool = True  # 社交关系路：关注作者动态（未登录跳过）
    edgerank_recall_social_limit: int = 100
    edgerank_recall_topic_enabled: bool = True  # 内容标签/分类路：偏好话题动态（未登录跳过）
    edgerank_recall_topic_limit: int = 100
    edgerank_recall_geo_enabled: bool = True  # 地理位置路：附近动态（需请求带 lat/lng）
    edgerank_recall_geo_limit: int = 50
    edgerank_recall_geo_radius_km: float = 50.0  # 附近范围半径
    edgerank_recall_cf_enabled: bool = True  # 协同过滤路（MVP 近似）：点赞/互动过作者的新动态
    edgerank_recall_cf_limit: int = 100

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
    # 评论举报阈值：单条评论累计有效举报数（按 rpid+report_mid 去重后）达到该值，
    # 评论 state 由 normal → auditing（进入审核，仅作者可见），交管理员复核。
    comment_report_threshold: int = 3
    # 统一举报（2.14.0）达阈值转审核：动态 / 用户空间举报累计有效举报数（按
    # (reportMid, bizType, bizId) 去重后）达到该值时，把被举报对象转 auditing 待复核。
    report_threshold: int = 3
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
    # Casdoor 管理员账号（password grant 换取 admin token 查询用户信息）。
    # Casdoor 若开启「不公开账户信息」，service 模式（clientId/clientSecret）
    # 查不到用户，必须用管理员登录后的 token（Bearer 用户模式）查询。
    # 注意：admin 走的是 Casdoor 内置 application「app-built-in」，其 clientId /
    # clientSecret 与普通登录应用不同，需单独配置；application 名固定为 app-built-in。
    casdoor_admin_name: str = ""
    casdoor_admin_password: str = ""
    casdoor_admin_client_id: str = ""
    casdoor_admin_client_secret: str = ""
    casdoor_admin_application: str = "app-built-in"

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

    @property
    def faststream_log_level_int(self) -> int:
        """FastStream 日志级别字符串对应的 logging 级别整数（非法值回退 INFO）。"""
        import logging

        return getattr(logging, self.faststream_log_level.upper(), logging.INFO)

    def model_post_init(self, __context) -> None:
        """.env 中证书的换行符是字面 \n，需转为真实换行符才能被 PEM 解析。"""
        if self.casdoor_certificate and "\\n" in self.casdoor_certificate:
            self.casdoor_certificate = self.casdoor_certificate.replace("\\n", "\n")
        # 防御：连接池总上限（pool_size + max_overflow）不得超过安全余量（100）。
        # 曾出现 .env 误配 mysql_pool_size=10000 → 进程疯狂建连接打满 MySQL(1040 Too many connections)。
        if self.mysql_pool_size + self.mysql_max_overflow > 100:
            self.mysql_pool_size = 20
            self.mysql_max_overflow = 30

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
