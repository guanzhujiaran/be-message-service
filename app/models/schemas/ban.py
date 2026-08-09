"""封禁接口请求 / 响应体（与 `msg_user_ban` 表解耦）。

审核评论 / 私信时，管理端可对违规用户批量封禁（按服务维度）或解封，
并支持查询某用户当前封禁状态、分页查看封禁记录列表。
"""

from datetime import datetime

from sqlmodel import Field, SQLModel

from app.models.enums import BanDurationTypeEnum, BanServiceEnum, BanStatusEnum


class BanCreateReq(SQLModel):
    """批量封禁请求（审核联动）。

    支持一次封禁多个用户、多个服务；`duration_type` 决定限时或永久，
    限时封禁必须给出 `duration_days`（>=1）。
    """

    mids: list[int] = Field(description="待封禁用户 mid 列表（可批量）")
    ban_services: list[str] = Field(
        description="封禁的服务范围：comment 评论 / dm 私信（可多选）"
    )
    reason: str = Field(min_length=1, max_length=512, description="封禁理由（必填）")
    duration_type: BanDurationTypeEnum = Field(
        description="temporary 限时 / permanent 永久"
    )
    duration_days: int | None = Field(
        default=None, ge=1, description="限时封禁天数（duration_type=temporary 时必填）"
    )


class UnbanReq(SQLModel):
    """批量解封请求。"""

    mids: list[int] = Field(description="待解封用户 mid 列表（可批量）")
    ban_services: list[str] | None = Field(
        default=None,
        description="仅解封指定服务（缺省表示解封该用户全部服务封禁）",
    )


class BanItem(SQLModel):
    """单条封禁记录（列表 / 回显用）。"""

    id: int
    mid: int
    ban_services: list[str]
    reason: str
    duration_type: BanDurationTypeEnum
    duration_days: int | None
    banned_until: datetime | None
    operator_mid: int
    status: BanStatusEnum
    created_at: datetime
    updated_at: datetime


class BanListResp(SQLModel):
    """封禁记录分页列表。"""

    items: list[BanItem] = Field(default_factory=list)
    total: int = 0
    page_num: int = 1
    page_size: int = 20


class BanStatusResp(SQLModel):
    """某用户的封禁状态汇总（按服务维度）。"""

    mid: int
    banned: bool = Field(description="是否存在任一服务生效中的封禁")
    services: dict[str, "BanServiceStatus"] = Field(
        default_factory=dict,
        description="服务 → 该服务封禁状态（仅含被封禁的服务）",
    )


class BanServiceStatus(SQLModel):
    """单个服务的封禁状态明细。"""

    banned: bool = Field(description="该服务是否当前生效中")
    status: BanStatusEnum = Field(description="记录状态：active / lifted")
    reason: str | None = None
    duration_type: BanDurationTypeEnum | None = None
    banned_until: datetime | None = None
    operator_mid: int | None = None


# 让 BanStatusResp.services 的嵌套类型可被 SQLModel 正确识别
BanStatusResp.model_rebuild()


__all__ = [
    "BanCreateReq",
    "UnbanReq",
    "BanItem",
    "BanListResp",
    "BanStatusResp",
    "BanServiceStatus",
]
