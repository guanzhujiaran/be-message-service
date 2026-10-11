"""运行时系统配置（`msg_sys_config`）的接口模型与配置值校验模型（2.64.0）。

两类模型职责不同：

- **配置值模型**（`CommentRateRule` / `CommentRateLimitConfig`）：约束 JSON `value` 的结构，
  写入前校验（非法值拒绝入库）、读取后校验（脏值回落 settings 默认）；
- **接口模型**（`SysConfigItem` / `SysConfigUpdateReq`）：管理端读写用的载体，`value` 保持
  宽松 `dict`——校验按 `key` 派发（见 `app/services/common/runtime_config.py`），
  这样新增配置项不必改接口层。
"""

from datetime import datetime

from bili_common.core.time_utils import OptionalUnixTime
from sqlmodel import Field, SQLModel

from app.core.config import settings
from app.models.schemas.base import SnowflakeInt, auto_str

# ==================== 配置值模型 ====================


class CommentRateRule(SQLModel):
    """单条评论频率规则：`window_seconds` 秒内最多 `max_count` 条。"""

    window_seconds: int = Field(gt=0, description="统计窗口（秒）")
    max_count: int = Field(ge=1, description="窗口内允许的最大条数")
    same_content: bool = Field(
        default=False,
        description="True=只统计正文相同的评论（防重复刷屏）；False=统计窗口内全部评论",
    )


class CommentRateLimitConfig(SQLModel):
    """评论频率限制配置：两档阈值同 key 存储，保证原子更新。

    `root` = 一级评论（广场刷屏的主要目标，更严）；`reply` = 楼中楼回复
    （连回多人属正常行为，更宽松）。任一档为空列表表示**关闭该档限流**。
    """

    root: list[CommentRateRule] = Field(
        default_factory=list, description="一级评论阈值（[]=关闭限流）"
    )
    reply: list[CommentRateRule] = Field(
        default_factory=list, description="楼中楼回复阈值（[]=关闭限流）"
    )


# ==================== 推荐机器学习配置（2.66.0）====================


class FeedMLConfig(SQLModel):
    """推荐「机器学习三件套」的运行时开关与权重。

    三件套（LightGBM 精排 / ALS 协同召回 / LinUCB 上下文探索）**默认全关**，
    需训练产出模型后由管理端在本项打开；权重可随时热调以便灰度：

    - ``ranker_enabled`` + ``ranker_blend_weight``：LightGBM 与 EdgeRank 分的融合
      （``weight`` 是模型话语权，0=纯规则）；
    - ``als_enabled`` + ``als_limit``：ALS 协同召回一路及其条数上限；
    - ``linucb_enabled`` + ``linucb_weight`` + ``linucb_alpha``：上下文老虎机探索
      （``weight`` 探索强度、``alpha`` 探索系数）。
    """

    # 默认值一律 default_factory 引用 settings：**settings 是唯一权威默认值来源**。
    # 否则 DB 里只写了一部分字段时，补齐出来的是这里的字面量、与 settings 漂移。
    ranker_enabled: bool = Field(
        default_factory=lambda: settings.ml_ranker_enabled,
        description="LightGBM 精排开关（需先产出模型文件）",
    )
    ranker_blend_weight: float = Field(
        default_factory=lambda: settings.ml_ranker_blend_weight,
        ge=0.0,
        le=1.0,
        description="模型分话语权（0=纯 EdgeRank）",
    )
    als_enabled: bool = Field(
        default_factory=lambda: settings.ml_als_enabled,
        description="ALS 协同召回开关（需先产出索引文件）",
    )
    als_limit: int = Field(
        default_factory=lambda: settings.ml_als_limit,
        ge=1,
        le=1000,
        description="ALS 召回条数上限",
    )
    linucb_enabled: bool = Field(
        default_factory=lambda: settings.ml_linucb_enabled,
        description="LinUCB 上下文探索开关（需先回放训练臂参数）",
    )
    linucb_weight: float = Field(
        default_factory=lambda: settings.ml_linucb_weight,
        ge=0.0,
        le=1.0,
        description="探索分叠加强度（0=关闭）",
    )
    linucb_alpha: float = Field(
        default_factory=lambda: settings.ml_linucb_alpha,
        ge=0.0,
        le=5.0,
        description="UCB 探索系数（越大越探索）",
    )


# ==================== 管理端接口模型 ====================


@auto_str
class SysConfigItem(SQLModel):
    """单个运行时配置项（管理端读）。

    `isDefault=True` 表示该 key 尚未写入 `msg_sys_config`，当前生效的是 settings 默认值
    （管理端据此展示「默认值 / 已覆盖」状态，并可把默认值直接改为自定义值）。
    """

    key: str
    value: dict
    remark: str | None = None
    updatedBy: SnowflakeInt = Field(
        default=0, description="最后修改者 mid（字符串版见 updatedBy_str）"
    )
    updated_at: OptionalUnixTime = Field(default=None, description="最后更新时间（秒级时间戳）")
    isDefault: bool = Field(
        default=False, description="是否仍是 settings 默认值（未写入 DB）"
    )


class SysConfigListResp(SQLModel):
    """运行时配置列表（管理端读）。"""

    items: list[SysConfigItem] = Field(default_factory=list)


class SysConfigUpdateReq(SQLModel):
    """写入运行时配置（整体替换该 key 的值）。"""

    key: str = Field(max_length=64, description="配置键（必须是已登记的可热更新项）")
    value: dict = Field(description="配置值（JSON，整体替换；按 key 派发校验）")
    remark: str | None = Field(default=None, max_length=255, description="备注")


__all__ = [
    "CommentRateLimitConfig",
    "CommentRateRule",
    "FeedMLConfig",
    "SysConfigItem",
    "SysConfigListResp",
    "SysConfigUpdateReq",
]
