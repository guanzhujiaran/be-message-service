"""运行时系统配置（`msg_sys_config`）的接口模型与配置值校验模型（2.64.0）。

两类模型职责不同：

- **配置值模型**（`CommentRateRule` / `CommentRateLimitConfig`）：约束 JSON `value` 的结构，
  写入前校验（非法值拒绝入库）、读取后校验（脏值回落 settings 默认）；
- **接口模型**（`SysConfigItem` / `SysConfigUpdateReq`）：管理端读写用的载体，`value` 保持
  宽松 `dict`——校验按 `key` 派发（见 `app/services/common/runtime_config.py`），
  这样新增配置项不必改接口层。
"""

from datetime import datetime

from sqlmodel import Field, SQLModel


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


# ==================== 管理端接口模型 ====================


class SysConfigItem(SQLModel):
    """单个运行时配置项（管理端读）。

    `isDefault=True` 表示该 key 尚未写入 `msg_sys_config`，当前生效的是 settings 默认值
    （管理端据此展示「默认值 / 已覆盖」状态，并可把默认值直接改为自定义值）。
    """

    key: str
    value: dict
    remark: str | None = None
    updatedBy: int = Field(default=0, description="最后修改者 mid")
    updated_at: datetime | None = Field(default=None, description="最后更新时间")
    isDefault: bool = Field(default=False, description="是否仍是 settings 默认值（未写入 DB）")


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
    "SysConfigItem",
    "SysConfigListResp",
    "SysConfigUpdateReq",
]
