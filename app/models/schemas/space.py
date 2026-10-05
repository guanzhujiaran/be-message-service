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
class SpacePrivacyFlags(SQLModel):
    """空间对外可见性开关（2.58.0）。

    两类语义，前端消费方式不同：

    - **敏感原文类**（``show_personal_info`` / ``show_login_info``）：后端在
      `viewer != mid` 且开关关闭时**直接不下发对应字段**（不是null），前端无需
      区分「无权限」与「无数据」；
    - **列表/条目类**（``show_follow_list`` / ``show_like_list`` /
      ``show_fans_list``）：后端在关闭时让该类型内容**整类不出现**。

    ``show_favorites`` 复用既有 `TUserFavoriteSetting.showFavorites`（收藏夹整体
    对外可见性），不另建列，故一并回显方便设置页统一渲染。
    """

    show_personal_info: bool = Field(
        default=False, description="个人资料：性别/生日/邮箱是否对外"
    )
    show_login_info: bool = Field(
        default=False, description="上次登录时间与IP属地是否对外"
    )
    show_follow_list: bool = Field(
        default=False, description="时间线「我关注了谁」条目是否对外"
    )
    show_like_list: bool = Field(
        default=False, description="时间线「我点赞了什么」条目是否对外"
    )
    show_fans_list: bool = Field(default=False, description="粉丝列表是否对外")
    show_favorites: bool = Field(
        default=True, description="收藏夹是否对外（复用 TUserFavoriteSetting）"
    )


@auto_str
class SpacePrivacyUpdateReq(SQLModel):
    """空间可见性开关更新请求（当前登录用户自己的设置，2.58.0）。

    五个开关整体提交（全量覆盖），不做 PATCH 语义 —— 前端设置页一次保存全部开关，
    避免「部分更新」与「默认值」混淆。
    """

    show_personal_info: bool = Field(
        default=False, description="个人资料：性别/生日/邮箱是否对外"
    )
    show_login_info: bool = Field(
        default=False, description="上次登录时间与IP属地是否对外"
    )
    show_follow_list: bool = Field(
        default=False, description="时间线「我关注了谁」条目是否对外"
    )
    show_like_list: bool = Field(
        default=False, description="时间线「我点赞了什么」条目是否对外"
    )
    show_fans_list: bool = Field(default=False, description="粉丝列表是否对外")


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

    # ---- 2.58.0 隐私与对外可见性 ----
    # 邮箱：pptr TUserDetail.email 有数据源，但历来不在空间资料里下发；
    # 受 show_personal_info 控制（viewer==self 时恒下发）
    email: str | None = None
    # 上次登录时间 + IP 属地（属地由 mmdb 读取时实时解析，不入库）；
    # 受 show_login_info 控制。last_login_at 为 pptr TUserActInfoLog 最近一条
    # 登录记录的createdAt。
    last_login_at: datetime | None = None
    ip_location: str | None = None
    # 空间被访问次数（2.58.0：进入他人空间时按 bizType=USER 上报）。
    # 统计数字固定展示、不设开关。
    view_count: int = 0
    privacy_flags: SpacePrivacyFlags = Field(default_factory=SpacePrivacyFlags)


@auto_str
class SpaceTimelineItem(SQLModel):
    """行为时间线单条记录（2.58.0 计划书 §3.3）。

    反映「当前有效状态」而非操作流水：取消点赞 / 取关 / 取消收藏后，对应条目
    因明细行被物理删除而自然消失。出参字段统一为：

    - `act_type`：follow / like / favorite；
    - follow：`target_mid/name/face` = 被关注用户；
    - like / favorite：`target_dyn_id` = 动态 id，`target_mid/name/face` = 动态作者，
      `target_text` = 动态正文摘要；favorite 额外带 `target_folder_name`。
    """

    act_type: str = Field(
        description="行为类型：follow 关注 / like 点赞 / favorite 收藏"
    )
    target_mid: int | None = Field(
        default=None,
        description="目标用户 mid（follow=被关注者；like/favorite=动态作者）",
    )
    target_name: str | None = Field(
        default=None, description="目标用户展示名（对方注销或回查失败时为 null）"
    )
    target_face: str | None = Field(default=None, description="目标用户头像 URL")
    target_text: str | None = Field(
        default=None, description="动态正文摘要（仅 like / favorite 有）"
    )
    target_dyn_id: int | None = Field(
        default=None,
        description="动态 id（仅 like / favorite 有，前端点进 MOMENT_DETAIL）",
    )
    target_folder_name: str | None = Field(
        default=None, description="收藏夹名称（仅 favorite 有）"
    )
    acted_at: datetime = Field(description="行为发生时间（created_at）")


@auto_str
class SpaceTimelineResp(SQLModel):
    """行为时间线分页响应（游标 = 本页最后一条的 acted_at）。"""

    items: list[SpaceTimelineItem] = Field(default_factory=list)
    has_more: bool = Field(default=False)
    cursor: str | None = Field(
        default=None, description="下一页游标（ISO 时间），has_more=false 时为空"
    )


@auto_str
class SpaceViewHistoryItem(SQLModel):
    """浏览历史单条记录（数据源 `TInteractionViewLog`，每用户每资源一行合并）。"""

    biz_type: str = Field(
        description="资源类型文字（dynamic / lottery / rpa_* / user）"
    )
    biz_id: int = Field(description="资源 id（dynamic 时 = dynId）")
    title: str | None = Field(
        default=None,
        description="资源标题/正文摘要（当前仅 dynamic 回查，其余 bizType 待 P3）",
    )
    last_view_at: datetime = Field(description="最后访问时间")
    view_count: int = Field(default=1, description="该用户对该资源的累计浏览次数")


@auto_str
class SpaceViewHistoryResp(SQLModel):
    """浏览历史分页响应（游标 = 本页最后一条的 last_view_at）。"""

    items: list[SpaceViewHistoryItem] = Field(default_factory=list)
    has_more: bool = Field(default=False)
    cursor: str | None = Field(
        default=None, description="下一页游标（ISO 时间），has_more=false 时为空"
    )


__all__ = [
    "SpacePrivacyFlags",
    "SpacePrivacyUpdateReq",
    "SpaceFollowStat",
    "SpaceInfoResp",
    "SpaceOfficial",
    "SpaceTimelineItem",
    "SpaceTimelineResp",
    "SpaceUpStat",
    "SpaceViewHistoryItem",
    "SpaceViewHistoryResp",
    "SpaceVip",
    "SpaceVipLabel",
    "SpaceVipWrap",
]
