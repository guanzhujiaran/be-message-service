"""关注 / 拉黑接口请求 / 响应体（与 `msg_user_follow` 表解耦）。

用户可关注 / 取关其他用户，也可拉黑 / 解除拉黑。用户展示信息（昵称 / 头像 /
等级 / 大会员等）**不在本服务冗余**：列表接口在装配时经 `PptrUser.get_many`
按 mid 批量回查 pptr 主数据并内联到 `item.user`（弱依赖，注销 / 回查失败
时为 `null`，前端渲染「账号已注销」）；黑名单列表读取不做用户存在性过滤，
已注销用户的 `mid` 仍会原样返回。
"""

from datetime import datetime

from bili_common.core.time_utils import UnixTime
from sqlmodel import Field, SQLModel

from app.models.enums import FollowStatusEnum
from app.models.schemas.base import auto_str
from app.models.schemas.user_brief import UserBriefOut
from app.models.str_int import StrInt

# ==================== 请求体 ====================


class FollowReq(SQLModel):
    """关注 / 取关 请求。"""

    target_mid: StrInt = Field(
        description="被关注的用户 mid（雪花 ID，StrInt 兼容前端 str 传参）"
    )


class BlockReq(SQLModel):
    """拉黑 / 解除拉黑 请求。"""

    target_mid: StrInt = Field(
        description="被拉黑的用户 mid（雪花 ID，StrInt 兼容前端 str 传参）"
    )


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
class FollowUserBrief(SQLModel):
    """关注 / 粉丝 / 黑名单列表里的**用户卡片**（对标 B 站 `x/relation/followings` 的 list 项）。

    只保留卡片渲染需要的展示字段，不再直接复用 `UserBriefOut` —— 后者还带铭牌 / 官标 /
    关注数等列表用不到的字段（且含私域字段，靠序列化期裁剪），对列表来说太重。
    数据仍由 `PptrUser.get_many` 批量回查（一次 IN，无 N+1）；注销 / 回查失败时为 `None`。
    """

    mid: int = Field(description="对方用户 mid")
    name: str | None = Field(default=None, description="昵称")
    face: str | None = Field(default=None, description="头像链接")
    sign: str | None = Field(default=None, description="个性签名")
    level: int = Field(default=0, description="等级（0~6）")
    vip_status: str | None = Field(default=None, description="大会员状态")
    vip_type: int = Field(default=0, description="大会员类型")

    @classmethod
    def from_brief(cls, brief: UserBriefOut | None) -> "FollowUserBrief | None":
        """由 pptr 完整简档裁剪而成（`brief=None` = 已注销 / 回查失败，原样返回 None）。"""
        if brief is None:
            return None
        return cls(
            mid=int(brief.mid),
            name=brief.name or brief.uname,
            face=brief.face or brief.avatar,
            sign=brief.sign,
            level=int(brief.level or 0),
            vip_status=brief.vip_status,
            vip_type=int(brief.vip_type or 0),
        )


@auto_str
class FollowListItem(SQLModel):
    """关注 / 粉丝列表单条记录。"""

    mid: int = Field(description="对方用户 mid")
    # 我关注对方 / 对方关注我的时间，按方向填充
    created_at: UnixTime = Field(description="关系建立时间（秒级时间戳）")
    # 是否互相关注（仅在「我的关注」列表中需要时填充）
    mutual: bool = Field(default=False, description="是否互相关注")
    # 对方展示信息：服务端经 PptrUser.get_many 批量回查后裁剪成卡片模型；
    # 对方已注销 / pptr 回查失败时为 null，前端按「账号已注销」降级渲染
    user: FollowUserBrief | None = Field(
        default=None,
        description="对方展示信息（注销或回查失败时为 null）",
    )


FollowListResp.model_rebuild()


# ==================== 关注分组相关（对标 B 站 x/relation/tags） ====================


class FollowTagCreateReq(SQLModel):
    """新建关注分组请求。"""

    name: str = Field(max_length=20, description="分组名（B 站上限 20 字）")
    tip: str | None = Field(
        default=None, max_length=100, description="分组简介（选填）"
    )


class FollowTagAssignReq(SQLModel):
    """把某个关注对象写入 / 移出分组（覆盖式：以本次提交的分组集合为准）。"""

    target_mid: StrInt = Field(description="被分组的用户 mid")
    tag_ids: list[int] = Field(
        default_factory=list,
        description="该用户所属分组 id 集合（空数组=移出全部分组）；-10 表示特别关注",
    )


class FollowTagResp(SQLModel):
    """分组项（形状对齐 B 站 `data: [{tagid, name, count, tip}]`）。"""

    tagid: int = Field(description="分组 id：-10 特别关注 / 0 默认分组 / >0 自建分组")
    name: str = Field(description="分组名")
    count: int = Field(default=0, description="该分组下的关注数")
    tip: str = Field(default="", description="分组简介（系统分组为内置说明，无则空串）")


__all__ = [
    "BlockReq",
    "FollowCountResp",
    "FollowListItem",
    "FollowListResp",
    "FollowOpResp",
    "FollowRelationResp",
    "FollowReq",
    "FollowTagAssignReq",
    "FollowTagCreateReq",
    "FollowTagResp",
    "FollowUserBrief",
]
