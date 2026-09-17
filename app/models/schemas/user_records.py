"""用户中心「我的记录」模块的请求 / 响应模型。

对应 `pptr_user_gateway.py` 中 `GET /api/v1/user/act-log` 与
`GET /api/v1/user/exp-record` 两个只读查询接口（仅 be-message 使用，不放 bili-common）。

数据来源：
- 登录记录：pptr Postgres `TUserActInfoLog`（PptrUserActInfoLog）
- 经验记录：pptr Postgres `TUserExpRecord`（PptrUserExpRecord）

时间窗口：仅返回最近 7 天的记录，分页采用 `offset + limit`，`has_more` 指示是否还有下一页。
"""

from datetime import datetime

from sqlmodel import Field, SQLModel


from app.models.schemas.base import auto_str
@auto_str
class UserActLogItem(SQLModel):
    """单条登录 / 行为记录（TUserActInfoLog）。"""

    time: datetime = Field(description="行为发生时间（timezone-aware）")
    ip: str = Field(default="", description="客户端 IP（脱敏后，IPv4→a.b.*.*，IPv6→a:b:*）")
    location: str = Field(default="未知", description="地理位置（地区 + 运营商，如：中国 上海 移动）")
    ua: str = Field(default="", description="客户端 User-Agent")
    act_info: str = Field(default="login_succ", description="行为类型：login_succ / reg")


@auto_str
class UserActLogListResp(SQLModel):
    """登录记录列表（最近 7 天）。"""

    items: list[UserActLogItem] = Field(default_factory=list)
    has_more: bool = Field(default=False, description="是否还有下一页")


@auto_str
class UserExpRecordItem(SQLModel):
    """单条经验变动记录（TUserExpRecord）。"""

    time: datetime = Field(description="经验增加时间（timezone-aware）")
    action_type: int = Field(description="行为类型 int（对应 ExpActionType：1=daily_login）")
    action_name: str = Field(default="", description="行为类型名称（daily_login 等）")
    exp: int = Field(default=0, description="本次增加的经验值")
    ref_date: str = Field(default="", description="行为引用日期 YYYY-MM-DD")


@auto_str
class UserExpRecordListResp(SQLModel):
    """经验记录列表（最近 7 天）。"""

    items: list[UserExpRecordItem] = Field(default_factory=list)
    has_more: bool = Field(default=False, description="是否还有下一页")


__all__ = [
    "UserActLogItem",
    "UserActLogListResp",
    "UserExpRecordItem",
    "UserExpRecordListResp",
]
