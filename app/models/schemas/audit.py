"""管理端审核项的「内容来源」模型。

审核队列里只有 `rpid` / `msgkey` 这类裸 ID，管理员无法判断这条内容出自哪里，
更没法快速回到原始现场核对上下文。`AuditSourceInfo` 把来源统一收敛成一个结构：

- `label`        ：人类可读的来源名（前端直接展示成可点击文案）；
- `url`          ：站内相对路径，前端 `router.push` 即可跳转；
- `external_url` ：站外原始内容链接（B 站动态 / 专栏等），前端新开标签页；
- `params`       ：附加定位参数（rpid / msgkey / session_key），供前端高亮或深链。

`url` 与 `external_url` 至少有一个非空时前端才渲染成可点击链接，
两者皆空表示该来源暂无可跳转的落地页（仅展示 label）。
"""

from typing import Any

from bili_common.models import InteractionBizTypeEnum
from sqlmodel import Field, SQLModel


from app.models.schemas.base import auto_str
@auto_str
class AuditSourceInfo(SQLModel):
    """审核项的内容来源（供管理端点击直达原始内容）。"""

    kind: str = Field(description="来源大类：comment 评论 / dm 私信")
    biz_type: str = Field(
        description="业务类型：评论为 InteractionBizTypeEnum，私信固定为 dm"
    )
    label: str = Field(description="来源展示文案，如「抽奖卡片 #123」")
    oid: str | None = Field(default=None, description="业务实体id（字符串），私信为空")
    up_mid: int | None = Field(default=None, description="内容作者mid（评论区 UP 主）")
    url: str | None = Field(
        default=None, description="站内相对路径，前端可直接 router.push 跳转"
    )
    external_url: str | None = Field(
        default=None, description="站外原始内容链接（B 站动态 / 专栏）"
    )
    params: dict[str, str] = Field(
        default_factory=dict, description="附加定位参数（rpid / msgkey / session_key）"
    )


@auto_str
class AuditApproveReq(SQLModel):
    """通用审核通过请求（计划书 §5.13）：按 `bizType` + `bizId` 定位资源。"""

    bizType: InteractionBizTypeEnum = Field(
        description="资源类型（InteractionBizTypeEnum 值）"
    )
    bizId: str = Field(description="资源 ID（字符串，避免 19 位雪花 ID 精度丢失）")
    remark: str | None = Field(default=None, description="审核备注")


@auto_str
class AuditRejectReq(SQLModel):
    """通用审核驳回请求（计划书 §5.13）：按 `bizType` + `bizId` 定位资源。"""

    bizType: InteractionBizTypeEnum = Field(
        description="资源类型（InteractionBizTypeEnum 值）"
    )
    bizId: str = Field(description="资源 ID（字符串，避免 19 位雪花 ID 精度丢失）")
    rejectReason: str = Field(description="驳回原因（通知作者）")
    remark: str | None = Field(default=None, description="审核备注")


@auto_str
class AuditActionResp(SQLModel):
    """通用审核动作响应：回显定位键，操作结果细节由各资源自行返回。"""

    bizType: InteractionBizTypeEnum
    # bizIdStr 由 @auto_str 自动派生（禁止重复声明）
    bizId: int
    data: dict[str, Any] | None = Field(
        default=None, description="资源类返回的审核结果（各资源形态不同）"
    )


@auto_str
class AuditTypeCountRow(SQLModel):
    """byType 明细行：`type`/`total` 为公共列，状态列随域填充。

    各域只写自己的状态列（资源审核 auditing/normal/rejected/hidden，
    举报 pending/resolved/rejected），未涉及的列保持 None——
    路由配 `response_model_exclude_none=True` 后 None 列不出现在响应里。
    """

    type: str = Field(default="", description="子类型名（无子类型的域该行为空行/不产出）")
    total: int = 0
    auditing: int | None = Field(default=None, description="审核中（资源审核域）")
    normal: int | None = Field(default=None, description="已过审（资源审核域）")
    rejected: int | None = Field(default=None, description="已驳回（资源审核域 / 举报驳回）")
    hidden: int | None = Field(default=None, description="已下架（资源审核域）")
    deleted: int | None = Field(default=None, description="软删（当前仅评论域表达）")
    pending: int | None = Field(default=None, description="待处理（举报域）")
    resolved: int | None = Field(default=None, description="已成立（举报域）")


@auto_str
class AuditStatisticsResp(SQLModel):
    """通用审核统计响应（计划书 §5.13）：按业务域聚合的审核概览。

    - `byStatus` 键为各域状态名小写（与 AuditTypeCountRow 状态列同名）；
    - `byType` 仅在域有资源子类型维度时返回（动态 / 评论 / 举报），
      话题 / 头像 / 封面 / 私信无子类型，响应中省略该字段（只有 status 维度）。
    """

    total: int = 0
    byStatus: dict[str, int] = Field(default_factory=dict)
    byType: list[AuditTypeCountRow] | None = Field(
        default=None, description="按子类型分组明细；无子类型维度的域为 None（响应中省略）"
    )


__all__ = [
    "AuditSourceInfo",
    "AuditApproveReq",
    "AuditRejectReq",
    "AuditActionResp",
    "AuditStatisticsResp",
    "AuditTypeCountRow",
]
