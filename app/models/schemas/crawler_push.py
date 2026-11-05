"""管理端：be-crawler 爬虫「开始 / 结束」推送开关。

对应 crawler 侧 `Utils/推送/push_switch.py` 与
`/api/v1/background_service/BackgroundService/CrawlerPushSwitch*` 三个接口。

语义（与 crawler 完全一致）：

- 开关分「开始 / 结束」两个触发点，按 `crawler_name` 粒度配置；
- 生效优先级：本地文件里配置过的爬虫以文件为准（`source=file`），
  其余回退到环境变量 `CRAWLER_PUSH_START` / `CRAWLER_PUSH_END`（`source=env`），
  两者都没有则关闭；
- 默认全关，不影响爬虫本身的执行，只影响通知推送。
"""

from sqlmodel import Field, SQLModel

__all__ = [
    "CrawlerPushDeleteReq",
    "CrawlerPushEffectiveItem",
    "CrawlerPushItem",
    "CrawlerPushSaveReq",
    "CrawlerPushState",
    "CrawlerPushStateResp",
]


class CrawlerPushItem(SQLModel):
    """单个爬虫的开关（读写共用）"""

    crawler_name: str = Field(description="爬虫名，取值同 BackgroundServiceName")
    start: bool = Field(default=False, description="开始执行时是否推送")
    end: bool = Field(default=False, description="结束时是否推送")


class CrawlerPushEffectiveItem(SQLModel):
    """合并「文件 + 环境变量」之后的生效状态"""

    crawler_name: str = Field(description="爬虫名，取值同 BackgroundServiceName")
    start: bool = Field(default=False, description="开始执行时是否推送")
    end: bool = Field(default=False, description="结束时是否推送")
    source: str = Field(description="配置来源：file（接口写入）/ env（环境变量）")


class CrawlerPushState(SQLModel):
    """crawler 本地文件里持久化的内容"""

    items: list[CrawlerPushItem] = Field(
        default_factory=list,
        description="按爬虫配置的开关列表；未列出的爬虫回退到环境变量",
    )
    updated_at: int | None = Field(
        default=None, description="最近保存时间（秒级 unix 时间戳）"
    )


class CrawlerPushStateResp(SQLModel):
    """查询响应：文件内容 + 环境变量原值 + 每个爬虫的生效状态"""

    state: CrawlerPushState = Field(description="文件里持久化的内容")
    env_start: str = Field(default="", description="环境变量 CRAWLER_PUSH_START 原值")
    env_end: str = Field(default="", description="环境变量 CRAWLER_PUSH_END 原值")
    state_file: str = Field(default="", description="开关文件落盘路径（排查用）")
    effective: list[CrawlerPushEffectiveItem] = Field(
        default_factory=list, description="每个爬虫的最终生效状态"
    )


class CrawlerPushSaveReq(SQLModel):
    """保存请求：按 crawler_name 覆盖写入（未列出的爬虫保持原配置）"""

    items: list[CrawlerPushItem] = Field(description="要写入的开关列表，不能为空")


class CrawlerPushDeleteReq(SQLModel):
    """删除某个爬虫的文件配置，使其回退到环境变量"""

    crawler_name: str = Field(description="爬虫名，取值同 BackgroundServiceName")
