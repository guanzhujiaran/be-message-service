"""评论模块的请求 / 响应模型。

**所有 64 位 ID（rpid / oid / root / parent / dialog）在出参与入参中一律使用字符串**。
它们是雪花 ID，直接以 number 传给浏览器会触发 JS 的
`Number.MAX_SAFE_INTEGER`（2^53-1）精度丢失，导致点赞 / 回复串号。
服务内部仍以 int 运算，只在接口边界做转换。
"""

from datetime import datetime

from sqlmodel import Field, SQLModel

from app.models.enums import (
    CommentActionEnum,
    CommentStateEnum,
    CommentSubjectStateEnum,
    CommentTypeEnum,
)
from app.models.schemas.audit import AuditSourceInfo


# ==================== 公共片段 ====================


class CommentUserBrief(SQLModel):
    """评论卡片上展示的用户信息（直连 pptr Postgres 只读取得，本服务不冗余）。

    仅使用 pptr 现有四张表（TUserInfo / TUserDetail / TUserVip / TUserLevel）中**实际存在**
    的字段，**不新增任何表结构或表外字段**。参考 B 站 member 的展示结构，把数据库里
    已有但此前未返回的字段（大会员到期时间、经验值、角色、脱敏邮箱）一并补齐。
    """

    mid: int
    uname: str | None = None
    avatar: str | None = None
    level: int = 0
    vip_status: str | None = None
    vip_type: int = 0
    # 大会员到期时间（毫秒时间戳），来自 TUserVip.vip_due_date
    vip_due_date: int | None = None
    sex: str | None = None
    sign: str | None = None
    # 当前累积经验，来自 TUserLevel.current_exp
    exp: int | None = None
    # 角色标识（level0..level6 / root），来自 TUserInfo.role
    role: str | None = None
    # 脱敏后的邮箱，来自 TUserDetail.email
    email: str | None = None
    follower_count: int = 0
    following_count: int = 0
    like_count: int = 0
    nameplate_name: str | None = None
    nameplate_image: str | None = None
    nameplate_level: str | None = None
    official_title: str | None = None


class CommentItem(SQLModel):
    """一条评论的完整视图模型。"""

    rpid: str = Field(description="评论id（字符串）")
    oid: str = Field(description="所属业务实体id（字符串）")
    type: CommentTypeEnum

    mid: int = Field(description="发布者mid")
    member: CommentUserBrief | None = Field(
        default=None, description="发布者信息快照，快照缺失时为 null"
    )

    root: str = Field(default="0", description="根评论rpid，'0' 表示一级评论")
    parent: str = Field(default="0", description="父评论rpid，'0' 表示一级评论")
    dialog: str = Field(default="0", description="楼中楼会话串id")
    floor: int = Field(default=0, description="楼层号")
    reply_to: CommentUserBrief | None = Field(
        default=None, description="被回复者，楼中楼展示「回复 @xxx」"
    )

    message: str = Field(default="", description="正文，@ 以 @{mid} 占位符形式返回")
    pictures: list[str] = Field(default_factory=list, description="图片URL数组")
    at_users: list[CommentUserBrief] = Field(
        default_factory=list, description="被@用户，供前端把占位符渲染成链接"
    )
    emote_meta: dict | None = Field(default=None, description="表情包元信息")

    like_count: int = 0
    hate_count: int = 0
    rcount: int = Field(default=0, description="子评论总数")
    action: CommentActionEnum = Field(
        default=CommentActionEnum.NONE, description="当前登录用户的互动态：0无/1赞/2踩"
    )

    state: CommentStateEnum = CommentStateEnum.NORMAL
    is_top: bool = False
    is_essence: bool = False
    is_up_liked: bool = False

    # IP 已打码；前端优先展示 v6，无 v6 再回落 v4，两者皆空则不展示
    ip_v4_masked: str | None = None
    ip_v6_masked: str | None = None

    plat: str | None = Field(default=None, max_length=32, description="来源平台")
    device: str | None = Field(default=None, max_length=64, description="来源设备")

    ctime: datetime = Field(description="发布时间")

    replies: list["CommentItem"] = Field(
        default_factory=list, description="楼中楼预览（一级评论下最多 N 条）"
    )


# 自引用模型需显式重建，否则 replies 的前向引用不会被解析
CommentItem.model_rebuild()


# ==================== 发布 / 删除 ====================


class CommentAddReq(SQLModel):
    """发表评论。"""

    oid: str = Field(description="业务实体id（字符串）")
    type: CommentTypeEnum = Field(description="业务实体类型")
    root: str = Field(default="0", description="根评论rpid；发一级评论传 '0'")
    parent: str = Field(default="0", description="父评论rpid；发一级评论传 '0'")
    message: str = Field(
        min_length=1, description="正文；@ 用 @{mid} 占位符表达"
    )
    pictures: list[str] = Field(
        default_factory=list, description="图片URL数组，最多9张。仅存URL，服务端不转存"
    )
    at_mids: list[int] = Field(default_factory=list, description="被@用户的mid列表")
    emote_meta: dict | None = Field(default=None, description="表情包元信息")
    up_mid: int | None = Field(
        default=None,
        description="内容作者mid；评论区首次创建时用于确定置顶 / 管理权限",
    )


class CommentAddResp(SQLModel):
    rpid: str = Field(description="新评论id（字符串）")
    root: str = "0"
    parent: str = "0"
    state: CommentStateEnum = CommentStateEnum.NORMAL
    need_audit: bool = Field(
        default=False, description="是否进入待审核（仅作者本人可见）"
    )


class CommentDelReq(SQLModel):
    rpid: str = Field(description="待删除的评论id（字符串）")


class CommentOperationResp(SQLModel):
    affected: int = 0
    message: str = ""


# ==================== 列表 ====================


class CommentListResp(SQLModel):
    """一级评论列表。"""

    items: list[CommentItem] = Field(default_factory=list)
    top: CommentItem | None = Field(
        default=None, description="置顶评论，始终单独返回并置于列表顶部"
    )
    total: int = Field(default=0, description="一级评论总数（读评论区冗余计数）")
    all_count: int = Field(default=0, description="含楼中楼的评论总数")
    page_num: int = 1
    page_size: int = 20
    subject_state: CommentSubjectStateEnum = CommentSubjectStateEnum.NORMAL
    focus_rpid: str | None = Field(
        default=None,
        description="本次请求携带 focus_rpid 时回填，指向最终要定位的评论"
        "（可能是一级评论本身，也可能是楼中楼中的某条子评论）。",
    )
    focus_root: str | None = Field(
        default=None,
        description="focus_rpid 所属的根评论 rpid。当 focus 目标是楼中楼时，"
        "根评论会被提到列表顶部，前端据此展开楼中楼并滚动到 focus_rpid。",
    )


class CommentSubListResp(SQLModel):
    """楼中楼（子评论）列表。"""

    items: list[CommentItem] = Field(default_factory=list)
    root: str = Field(default="0", description="所属根评论rpid")
    total: int = Field(default=0, description="该根评论下的子评论总数")
    page_num: int = 1
    page_size: int = 20


class CommentCountResp(SQLModel):
    """评论区计数。"""

    oid: str
    type: CommentTypeEnum
    root_count: int = 0
    all_count: int = 0
    state: CommentSubjectStateEnum = CommentSubjectStateEnum.NORMAL


# ==================== 互动 ====================


class CommentActionReq(SQLModel):
    """点赞 / 点踩 / 取消。"""

    rpid: str = Field(description="评论id（字符串）")
    action: CommentActionEnum = Field(description="0取消 / 1点赞 / 2点踩")


class CommentActionResp(SQLModel):
    rpid: str
    action: CommentActionEnum = CommentActionEnum.NONE
    like_count: int = 0
    hate_count: int = 0


# ==================== 置顶 / 管理 ====================


class CommentTopReq(SQLModel):
    """置顶 / 取消置顶（内容作者或管理员）。"""

    oid: str = Field(description="业务实体id（字符串）")
    type: CommentTypeEnum = Field(description="业务实体类型")
    rpid: str = Field(description="要置顶 / 取消置顶的评论id（字符串，必须是根评论）")
    top: bool = Field(default=True, description="True 置顶 / False 取消置顶")


class CommentTopResp(SQLModel):
    top_rpid: str | None = Field(default=None, description="当前置顶评论id")
    success: bool = True


class CommentAuditReq(SQLModel):
    """管理端人工审核 / 上下架。"""

    rpid: str = Field(description="待处理评论id（字符串）")
    # 通过 / 驳回 / 下架 / 恢复
    op: str = Field(description="pass | reject | hidden | restore")
    note: str | None = Field(default=None, max_length=256, description="审核备注")


class CommentBulkAuditReq(SQLModel):
    """管理端批量人工审核 / 上下架。"""

    rpids: list[str] = Field(description="待处理评论id列表（字符串）")
    # 通过 / 驳回 / 下架 / 恢复
    op: str = Field(description="pass | reject | hidden | restore")
    note: str | None = Field(
        default=None, max_length=256, description="统一审核备注（所有条共用，可选）"
    )
    notes: dict[str, str] | None = Field(
        default=None,
        description="逐条审核备注：{ rpid(字符串): 原因 }，优先级高于 note；留空则该条用 note",
    )


class CommentBulkAuditResp(SQLModel):
    """批量审核结果汇总。"""

    total: int = Field(default=0, description="请求条数")
    success: int = Field(default=0, description="成功条数")
    failed: list[str] = Field(default_factory=list, description="失败的 rpid 列表")


class CommentAuditItem(SQLModel):
    """审核队列中的一条评论。"""

    rpid: str
    oid: str
    type: CommentTypeEnum
    mid: int
    message: str
    state: CommentStateEnum
    like_count: int = 0
    ctime: datetime
    ip_v4: str | None = None
    ip_v6: str | None = None
    plat: str | None = Field(default=None, max_length=32, description="来源平台")
    device: str | None = Field(default=None, max_length=64, description="来源设备")
    source: AuditSourceInfo | None = Field(
        default=None, description="内容来源，管理端可点击直达原始评论区"
    )
    member: CommentUserBrief | None = Field(
        default=None, description="发布者信息，装配时直连 pptr 只读取回"
    )


class CommentAuditListResp(SQLModel):
    items: list[CommentAuditItem] = Field(default_factory=list)
    total: int = 0
    page_num: int = 1
    page_size: int = 20
    states: list[CommentStateEnum] = Field(
        default_factory=list, description="本次实际生效的状态过滤（非 root 恒为待审核）"
    )
    can_view_all_states: bool = Field(
        default=False, description="当前管理员是否可查看全部状态（仅 root 为 True）"
    )


class CommentSourceResp(SQLModel):
    """一条评论的内容来源详情（管理端「内容来源」点击时按需拉取）。"""

    rpid: str
    root: str = "0"
    parent: str = "0"
    state: CommentStateEnum = CommentStateEnum.NORMAL
    source: AuditSourceInfo
    subject_state: CommentSubjectStateEnum | None = Field(
        default=None, description="所属评论区状态；评论区不存在时为 null"
    )
    root_count: int = Field(default=0, description="评论区一级评论数")
    all_count: int = Field(default=0, description="评论区评论总数（含楼中楼）")


class CommentAuditResp(SQLModel):
    rpid: str
    state: CommentStateEnum


class CommentStatsResp(SQLModel):
    """评论区全局统计（管理端）。"""

    total_comments: int = 0
    total_root: int = 0
    total_subjects: int = 0
    today_new: int = 0
    top_authors: list[CommentUserBrief] = Field(default_factory=list)
    # 各状态评论数（normal / auditing / rejected / hidden / deleted）；
    # total_comments 为该字典中「非 deleted」各项之和，确保驳回等状态被计入总数。
    state_counts: dict[str, int] = Field(default_factory=dict)


__all__ = [
    "CommentUserBrief",
    "CommentItem",
    "CommentAddReq",
    "CommentAddResp",
    "CommentDelReq",
    "CommentOperationResp",
    "CommentListResp",
    "CommentSubListResp",
    "CommentCountResp",
    "CommentActionReq",
    "CommentActionResp",
    "CommentAuditReq",
    "CommentAuditItem",
    "CommentAuditListResp",
    "CommentAuditResp",
    "CommentSourceResp",
    "CommentStatsResp",
]
