"""用户空间信息模块的请求 / 响应模型。

对标 B 站 `/x/space/wbi/acc/info` 的 `data` 结构（2.12.0 新增）：
- 有数据源字段映射 pptr 四张表（mid / name / sex / face / sign / level / birthday / vip）；
- `is_followed` 来自关注关系（FollowService.is_following）；
- `official` / `pendant` / `nameplate` / `top_photo` 等 pptr 无数据源字段返回空结构 / `null`，
  前端按空态兜底展示（见计划书 5.9 节 / 决策 20）。
"""

from datetime import datetime

from sqlmodel import Field, SQLModel


class SpaceOfficial(SQLModel):
    """官方认证信息（pptr 无数据源，固定返回空结构）。"""

    role: int = 0
    title: str = ""
    desc: str = ""
    type: int = -1


class SpaceVip(SQLModel):
    """大会员信息（映射 pptr TUserVip）。"""

    type: int = 0
    status: int = 0
    due_date: int = 0


class SpaceVipLabel(SQLModel):
    """大会员角标文案（pptr 无数据源，固定为空）。"""

    text: str = ""


class SpaceVipWrap(SQLModel):
    """大会员完整信息（对标 B 站 acc/info 的 vip 结构）。"""

    type: int = 0
    status: int = 0
    due_date: int = 0
    label: SpaceVipLabel = Field(default_factory=SpaceVipLabel)


class SpaceInfoResp(SQLModel):
    """用户空间完整资料（对标 B 站 `/x/space/wbi/acc/info` 的 data）。"""

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


__all__ = [
    "SpaceInfoResp",
    "SpaceOfficial",
    "SpaceVip",
    "SpaceVipLabel",
    "SpaceVipWrap",
]
