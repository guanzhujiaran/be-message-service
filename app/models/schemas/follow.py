"""关注 / 拉黑接口请求 / 响应体（与 `msg_user_follow` 表解耦）。

用户可关注 / 取关其他用户，也可拉黑 / 解除拉黑。用户展示信息（昵称 / 头像 /
等级 / 大会员等）**不在本服务冗余**：列表接口在装配时经 `PptrUser.get_many`
按 mid 批量回查 pptr 主数据并内联到 `item.user`（弱依赖，注销 / 回查失败
时为 `null`，前端渲染「账号已注销」）；黑名单列表读取不做用户存在性过滤，
已注销用户的 `mid` 仍会原样返回。
"""

from datetime import datetime

from sqlmodel import Field, SQLModel

from app.models.str_int import StrInt

from app.models.enums import FollowStatusEnum
from app.models.schemas.base import auto_str
from app.models.schemas.user_brief import UserBriefOut

# ==================== 请求体 ====================


class FollowReq(SQLModel):
    """关注 / 取关 请求。"""

    target_mid: StrInt = Field(description="被关注的用户 mid（雪花 ID，StrInt 兼容前端 str 传参）")


class BlockReq(SQLModel):
    """拉黑 / 解除拉黑 请求。"""

    target_mid: StrInt = Field(description="被拉黑的用户 mid（雪花 ID，StrInt 兼容前端 str 传参）")


# ==================== 响应体 ====================


@auto_str
class FollowOpResp(SQLModel):
    """关注 / 取关 / 拉黑 / 解除拉黑 等写操作的统一回执。"""

    mid: int = Field(description="操作发起者 mid")
    target_mid: int = Field(description="目标用户 mid")
    status: FollowStatusEnum | None = Field(
        default=None,
        description="操作完成后的当前关系：following 关注 / blocked 拉黑 / None 已无关系",
    )
    followed: bool = Field(default=False, description="操作后是否处于关注状态")
    blocked: bool = Field(default=False, description="操作后是否处于拉黑状态")


@auto_str
class FollowRelationResp(SQLModel):
    """我与某人的关系查询回执。

    方向说明：
    - `following`：我是否关注了对方；
    - `followed_by`：对方是否关注了我；
    - `mutual`：是否互相关注（双方都关注对方）；
    - `i_blocked`：我是否拉黑了对方；
    - `blocked_by`：对方是否拉黑了我。
    """

    mid: int = Field(description="发起查询的用户 mid")
    target_mid: int = Field(description="被查询的目标 mid")
    following: bool = Field(default=False, description="我是否关注了对方")
    followed_by: bool = Field(default=False, description="对方是否关注了我")
    mutual: bool = Field(default=False, description="是否互相关注")
    i_blocked: bool = Field(default=False, description="我是否拉黑了对方")
    blocked_by: bool = Field(default=False, description="对方是否拉黑了我")


@auto_str
class FollowCountResp(SQLModel):
    """关注 / 粉丝计数。"""

    mid: int
    following_count: int = Field(default=0, description="关注数")
    follower_count: int = Field(default=0, description="粉丝数")
    mutual_count: int = Field(default=0, description="互相关注数")


class FollowListResp(SQLModel):
    """关注 / 粉丝列表分页响应。

    `items` 仅含 `mid` 与关系建立时间，用户展示信息由前端按 mid 批量回查
    pptr 主数据，不在本服务冗余。
    """

    items: list["FollowListItem"] = Field(default_factory=list)
    total: int = 0
    page_num: int = 1
    page_size: int = 20


@auto_str
class FollowListItem(SQLModel):
    """关注 / 粉丝列表单条记录。"""

    mid: int = Field(description="对方用户 mid")
    # 我关注对方 / 对方关注我的时间，按方向填充
    created_at: datetime = Field(description="关系建立时间")
    # 是否互相关注（仅在「我的关注」列表中需要时填充）
    mutual: bool = Field(default=False, description="是否互相关注")
    # 对方展示信息（黑名单列表填充）：服务端经 PptrUser.get_many 批量回查后直接填入，
    # 私域字段由序列化期按访问者身份自动剥离；对方已注销 / pptr 回查失败时为 null，
    # 前端按「账号已注销」降级渲染
    user: UserBriefOut | None = Field(
        default=None,
        description="对方公开展示信息（他人可见字段；注销或回查失败时为 null）",
    )


FollowListResp.model_rebuild()


__all__ = [
    "BlockReq",
    "FollowCountResp",
    "FollowListItem",
    "FollowListResp",
    "FollowOpResp",
    "FollowRelationResp",
    "FollowReq",
]
