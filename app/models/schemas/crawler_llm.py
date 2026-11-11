"""管理端：be-crawler 云端 LLM 实例配置与运行统计（2.7x）。

对应 crawler 侧 `CONFIG.py::LLMApiConfig`（`settings.llm_apis`）与
`Service/llm_service/pool.py` 的 `/llm/config`、`/llm/stats` 能力。

数值一律以**字符串**传输（项目规范：前端 JS 无法安全表示大整数，且 crawler 侧
`requests_per_second` 是浮点，统一用 str 避免精度/类型歧义）。

`token` 契约（关键）：
- 读取时由 crawler 脱敏返回（`前4****后4`，长度 ≤8 时为 `****`）；
- 保存时若原样回传脱敏值或空串，服务端**保留原密钥**，不会把脱敏串写进配置。
"""

from sqlmodel import Field, SQLModel

__all__ = [
    "CrawlerLlmConfigItem",
    "CrawlerLlmConfigResp",
    "CrawlerLlmModelsReq",
    "CrawlerLlmModelsResp",
    "CrawlerLlmSaveItem",
    "CrawlerLlmSaveReq",
    "CrawlerLlmStatsItem",
    "CrawlerLlmTestReq",
    "CrawlerLlmTestResp",
]


class CrawlerLlmConfigItem(SQLModel):
    """单条 LLM 实例配置（读取态）。"""

    index: str = Field(description="实例在 crawler `llm_apis` 中的索引（从 0 开始）")
    base_url: str = Field(description="OpenAI 兼容接口地址（如 https://api.example.com/v1）")
    model_name: str = Field(description="模型名（如 deepseek-chat）")
    token: str = Field(description="脱敏后的密钥（前4****后4；长度 ≤8 时为 ****）")
    requests_per_second: str = Field(description="限流：每秒允许请求数（如 0.5 = 2 秒 1 次）")


class CrawlerLlmStatsItem(SQLModel):
    """单条 LLM 实例的运行统计（来自 crawler `/llm/stats`）。"""

    base_url: str = Field(description="实例接口地址（与配置项按 base_url + model_name 对应）")
    model_name: str = Field(description="模型名")
    state: str = Field(
        description="健康状态：healthy / cooling / quota_wait / suspended / removed"
    )
    available: bool = Field(description="当前是否可用（state == healthy）")
    disabled: bool = Field(description="是否因不可恢复错误被移出池（removed）")
    disabled_reason: str | None = Field(default=None, description="不可用原因（服务端文案）")
    invoke_count: str = Field(description="总调用次数")
    success_count: str = Field(description="成功次数")
    failure_count: str = Field(description="失败次数")
    consecutive_failures: str = Field(description="连续失败次数")
    success_rate: str = Field(description="成功率（0~1，两位小数；无调用时为 0.00）")
    rate_per_minute: str = Field(description="最近 60 秒内调用次数")
    avg_latency_seconds: str = Field(description="成功调用平均耗时（秒，三位小数）")
    total_tokens: str = Field(description="累计消耗 token 数")
    last_used_at: str | None = Field(
        default=None, description="最后一次调用时间（秒级 unix 时间戳）"
    )
    resume_at: str | None = Field(
        default=None, description="自动恢复时间（秒级 unix 时间戳）；null = 不会自动恢复"
    )
    last_error: str | None = Field(default=None, description="最近一次失败的错误信息")


class CrawlerLlmConfigResp(SQLModel):
    """配置 + 统计的组合响应（页面一次请求即可渲染表格）。"""

    items: list[CrawlerLlmConfigItem] = Field(description="当前生效的实例配置列表")
    stats: list[CrawlerLlmStatsItem] = Field(
        description="运行统计（按 base_url + model_name 与 items 对应，可能少于配置数）"
    )


class CrawlerLlmSaveItem(SQLModel):
    """单条 LLM 实例配置（保存态）。

    `token` 传空串或脱敏值（含 `****`）表示**沿用该索引上的原密钥**，
    仅在新增实例或换密钥时传明文。
    """

    base_url: str = Field(description="OpenAI 兼容接口地址")
    model_name: str = Field(description="模型名")
    token: str = Field(default="", description="明文密钥；空串/脱敏值 = 不修改原密钥")
    requests_per_second: str = Field(description="限流：每秒允许请求数（> 0）")


class CrawlerLlmSaveReq(SQLModel):
    """整体覆盖保存请求（与 crawler `PUT /llm/config` 语义一致）。"""

    items: list[CrawlerLlmSaveItem] = Field(description="完整的实例列表（顺序即索引顺序）")


class CrawlerLlmTestReq(SQLModel):
    """单条实例的连通性测试请求（管理端「测试」按钮，可测未保存的编辑态）。"""

    index: str | None = Field(
        default=None,
        description="该实例在当前列表中的索引（字符串）；token 为空/脱敏时按它回填原密钥，"
        "新增行传 null",
    )
    base_url: str = Field(description="OpenAI 兼容接口地址")
    model_name: str = Field(description="模型名")
    token: str = Field(default="", description="明文密钥；空串或脱敏值 = 沿用该索引上的原密钥")


class CrawlerLlmModelsReq(SQLModel):
    """单条实例的「获取模型列表」请求（管理端下拉候选，可对未保存的编辑态发起）。"""

    index: str | None = Field(
        default=None,
        description="该实例在当前列表中的索引（字符串）；token 为空/脱敏时按它回填原密钥，"
        "新增行传 null",
    )
    base_url: str = Field(description="OpenAI 兼容接口地址")
    model_name: str = Field(
        default="",
        description="该实例当前的模型名；仅用于「索引与配置是否匹配」的校验，可为空",
    )
    token: str = Field(default="", description="明文密钥；空串或脱敏值 = 沿用该索引上的原密钥")


class CrawlerLlmModelsResp(SQLModel):
    """一次模型列表查询的结果（诊断口径：不参与 crawler 的健康状态机、不改统计）。"""

    ok: bool = Field(description="是否查询成功（上游 /models 返回了可解析的列表）")
    base_url: str = Field(description="被查询的接口地址")
    status_code: str | None = Field(
        default=None, description="上游 HTTP 状态码；未发出请求时为 null"
    )
    latency_ms: str = Field(description="耗时（毫秒）；未发出请求时为 0")
    models: list[str] = Field(
        default_factory=list, description="上游可用的模型 id 列表（去重 + 升序）；失败时为空"
    )
    error: str | None = Field(default=None, description="失败或提示文案（已抹掉密钥，截断 200 字符）")


class CrawlerLlmTestResp(SQLModel):
    """一次探测的结果（诊断口径：不参与 crawler 的健康状态机、不改统计）。"""

    ok: bool = Field(description="是否探测成功（裸 ChatOpenAI 真实调用拿到模型回复）")
    base_url: str = Field(description="被测接口地址")
    model_name: str = Field(description="被测模型名")
    status_code: str | None = Field(
        default=None,
        description="上游 HTTP 状态码；SDK 成功调用拿不到状态码时记为 200，未发请求时为 null",
    )
    latency_ms: str = Field(description="耗时（毫秒）；未发请求时为 0")
    error: str | None = Field(default=None, description="失败原因（已抹掉密钥，截断 200 字符）")
