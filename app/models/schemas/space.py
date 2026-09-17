"""用户空间信息模块的请求 / 响应模型。

对标 B 站 `/x/space/wbi/acc/info` 的 `data` 结构（2.12.0 新增）：
- 有数据源字段映射 pptr 四张表（mid / name / sex / face / sign / level / birthday / vip）；
- `is_followed` 来自关注关系（FollowService.is_following）；
- `official` / `pendant` / `nameplate` / `top_photo` 等 pptr 无数据源字段返回空结构 / `null`，
  前端按空态兜底展示（见计划书 5.9 节 / 决策 20）。
"""

from datetime import datetime

from sqlmodel import Field, SQLModel


from app.models.schemas.base import auto_str
@auto_str
class SpaceOfficial(SQLModel):
    """官方认证信息（pptr 无数据源，固定返回空结构）。"""

    role: int = 0
    title: str = ""
    desc: str = ""
    type: int = -1


@auto_str
class SpaceVip(SQLModel):
    """大会员信息（映射 pptr TUserVip）。"""

    type: int = 0
    status: int = 0
    due_date: int = 0


@auto_str
class SpaceVipLabel(SQLModel):
    """大会员角标文案（pptr 无数据源，固定为空）。"""

    text: str = ""


@auto_str
class SpaceVipWrap(SQLModel):
    """大会员完整信息（对标 B 站 acc/info 的 vip 结构）。"""

    type: int = 0
    status: int = 0
    due_date: int = 0
    label: SpaceVipLabel = Field(default_factory=SpaceVipLabel)


@auto_str
class SpaceFollowStat(SQLModel):
    """关注 / 粉丝 / 互关计数（2.32.0：内联自 `GET /api/v1/message/follow/stat`）。

    与 `FollowCountResp` 字段一致，但**不带** `mid`（外层 `SpaceInfoResp.mid` 已冗余），
    避免同一份数据在响应里重复出现。
    """

    following_count: int = Field(default=0, description="关注数")
    follower_count: int = Field(default=0, description="粉丝数")
    mutual_count: int = Field(default=0, description="互相关注数")


@auto_str
class SpaceUpStat(SQLModel):
    """空间动态统计（2.32.0：内联自 `GET /api/v1/community/upstat`）。

    与 `MomentUpStatResp` 统计字段一致，同样**不带** `mid`。
    """

    dynamic_count: int = Field(default=0, description="对外可见动态总数")
    like_count: int = Field(default=0, description="这些动态被点赞的总数")


@auto_str
class SpaceInfoResp(SQLModel):
    """用户空间完整资料（对标 B 站 `/x/space/wbi/acc/info` 的 data）。

    2.32.0：新增 `follow_stat` / `upstat` 两个**只读派生聚合字段**，把原本需要
    `/message/follow/stat` + `/community/upstat` 两次额外请求才能拿到的统计
    一次带出（悬浮用户卡片 / 空间页由 3 次 HTTP 降为 1 次）。两个原端点保留不动。
    """

    mid: int
    name: str = ""
    sex: str = ""
    face: str | None = None
    sign: str = ""
    level: int = 0
    rank: int = 10000
    jointime: int = 0
    moral: int = 0
    silence: int = 0
    coins: int = 0
    birthday: str = ""
    official: SpaceOfficial = Field(default_factory=SpaceOfficial)
    vip: SpaceVipWrap = Field(default_factory=SpaceVipWrap)
    pendant: None = None
    nameplate: None = None
    top_photo: str | None = None
    is_followed: bool = False
    is_self: bool = False
    # 2.32.0：聚合统计（一次请求带出，避免前端悬浮卡片 / 空间页多次调用）
    follow_stat: SpaceFollowStat = Field(default_factory=SpaceFollowStat)
    upstat: SpaceUpStat = Field(default_factory=SpaceUpStat)


__all__ = [
    "SpaceFollowStat",
    "SpaceInfoResp",
    "SpaceOfficial",
    "SpaceUpStat",
    "SpaceVip",
    "SpaceVipLabel",
    "SpaceVipWrap",
]
