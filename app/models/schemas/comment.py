"""评论模块的请求 / 响应模型。

**所有 64 位 ID（rpid / oid / root / parent / dialog）在出参与入参中一律使用字符串**。
它们是雪花 ID，直接以 number 传给浏览器会触发 JS 的
`Number.MAX_SAFE_INTEGER`（2^53-1）精度丢失，导致点赞 / 回复串号。
服务内部仍以 int 运算，只在接口边界做转换。
"""

from datetime import datetime
from typing import Annotated

from sqlmodel import Field, SQLModel

from bili_common.models import InteractionBizTypeEnum
from app.models.enums import (
    CommentActionEnum,
    ResourceAuditStatusEnum,
    CommentSubjectStateEnum,
    MomentReportReasonEnum,
)
from app.models.schemas.audit import AuditSourceInfo
from app.models.schemas.base import AutoStrMixin
from app.models.schemas.user_brief import UserBriefOut
from app.models.schemas.visibility import Private, VisibilityMixin

# ==================== 公共片段 ====================


class CommentUserBrief(UserBriefOut):
    """评论卡片上展示的用户信息（直连 pptr Postgres 只读取得，本服务不冗余）。

    评论是他人可见内容，故沿用 :class:`UserBriefOut`：昵称 / 头像 / 等级等公开字段
    照常输出，``vip_due_date`` / ``exp`` / ``role`` / ``email`` 等私域字段由
    ``VisibilityMixin`` 在**序列化期**按访问者身份自动剥离（仅本人 / 管理员可见），
    装配层**无需**再手动投影。
    """


__all__ = ["CommentUserBrief"]


class CommentItem(SQLModel, AutoStrMixin):
    """一条评论的完整视图模型。"""

    rpid: str = Field(description="评论id（字符串）")
    oid: str = Field(description="所属业务实体id（字符串）")
    type: InteractionBizTypeEnum

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

    message: str = Field(
        default="", description="正文，@ 已渲染为 @昵称 文本（对齐 B 站 content.message）"
    )
    pictures: list[str] = Field(default_factory=list, description="图片URL数组")
    at_users: list[CommentUserBrief] = Field(
        default_factory=list, description="被@用户信息数组（对齐 B 站 content.members）"
    )
    # @ 昵称 → mid 映射（对齐 B 站 content.at_name_to_mid / at_name_to_mid_str）
    at_name_to_mid: dict[str, int] = Field(
        default_factory=dict, description="被@用户：昵称 → mid 映射"
    )
    at_name_to_mid_str: dict[str, str] = Field(
        default_factory=dict, description="被@用户：昵称 → mid 字符串映射"
    )
    # 话题元信息（对齐 B 站 content.topics_meta）：话题名 → { uri }
    topics_meta: dict[str, dict] = Field(
        default_factory=dict, description="正文里的 #话题# → 话题跳转 uri"
    )
    emote_meta: dict | None = Field(default=None, description="表情包元信息")

    like_count: int = 0
    hate_count: int = 0
    rcount: int = Field(default=0, description="子评论总数")
    action: CommentActionEnum = Field(
        default=CommentActionEnum.NONE, description="当前登录用户的互动态：0无/1赞/2踩"
    )

    state: ResourceAuditStatusEnum = ResourceAuditStatusEnum.NORMAL
    is_top: bool = False
    is_essence: bool = False
    is_up_liked: bool = False

    # IP 属地（服务端 GeoIP 解析，如「浙江 杭州」）+ 运营商 ISP
    ip_location: str | None = None
    ip_isp: str | None = None

    plat: str | None = Field(default=None, max_length=32, description="来源平台")
    device: str | None = Field(default=None, max_length=64, description="来源设备")

    ctime: datetime = Field(description="发布时间")

    replies: list["CommentItem"] = Field(
        default_factory=list, description="楼中楼预览（一级评论下最多 N 条）"
    )


# 自引用模型需显式重建，否则 replies 的前向引用不会被解析
CommentItem.model_rebuild()


# ==================== 发布 / 删除 ====================


class CommentAddReq(SQLModel, AutoStrMixin):
    """发表评论。"""

    oid: str = Field(description="业务实体id（字符串）")
    type: InteractionBizTypeEnum = Field(description="业务实体类型")
    root: str = Field(default="0", description="根评论rpid；发一级评论传 '0'")
    parent: str = Field(default="0", description="父评论rpid；发一级评论传 '0'")
    message: str = Field(
        min_length=1,
        description="正文；@ 以 @昵称 文本表达（对齐 B 站），服务端会按 at_name_to_mid 转为 @{mid} 占位符存储",
    )
    pictures: list[str] = Field(
        default_factory=list, description="图片URL数组，最多9张。仅存URL，服务端不转存"
    )
    at_mids: list[int] = Field(default_factory=list, description="被@用户的mid列表")
    # @ 昵称 → mid 映射（对齐 B 站 content.at_name_to_mid），服务端据此把正文里的 @昵称 归一为 @{mid}
    at_name_to_mid: dict[str, int] = Field(
        default_factory=dict, description="被@用户：昵称 → mid 映射"
    )
    emote_meta: dict | None = Field(default=None, description="表情包元信息")
    up_mid: int | None = Field(
        default=None,
        description="内容作者mid；评论区首次创建时用于确定置顶 / 管理权限",
    )


class CommentAddResp(SQLModel, AutoStrMixin):
    rpid: str = Field(description="新评论id（字符串）")
    root: str = "0"
    parent: str = "0"
    state: ResourceAuditStatusEnum = ResourceAuditStatusEnum.NORMAL
    need_audit: bool = Field(
        default=False, description="是否进入待审核（仅作者本人可见）"
    )


class CommentDelReq(SQLModel, AutoStrMixin):
    rpid: str = Field(description="待删除的评论id（字符串）")


class CommentOperationResp(SQLModel, AutoStrMixin):
    affected: int = 0
    message: str = ""


# ==================== 列表 ====================


class CommentListResp(SQLModel, AutoStrMixin):
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
    viewer_is_anonymous: bool = Field(
        default=False,
        description="当前请求是否为匿名访问（viewer_mid 缺失）。"
        "匿名时服务端已强制限制 page_size=10，前端应渲染登录引导蒙层。",
    )


class CommentSubListResp(SQLModel, AutoStrMixin):
    """楼中楼（子评论）列表。"""

    items: list[CommentItem] = Field(default_factory=list)
    root: str = Field(default="0", description="所属根评论rpid")
    total: int = Field(default=0, description="该根评论下的子评论总数")
    page_num: int = 1
    page_size: int = 20


class CommentCountResp(SQLModel, AutoStrMixin):
    """评论区计数。"""

    oid: str
    type: InteractionBizTypeEnum
    root_count: int = 0
    all_count: int = 0
    state: CommentSubjectStateEnum = CommentSubjectStateEnum.NORMAL


# ==================== 最新评论（首页，按资源类型分组） ====================

class CommentLatestGroup(SQLModel, AutoStrMixin):
    """某资源类型下的最新根评论分组（首页「最新评论」用）。"""

    type: InteractionBizTypeEnum = Field(description="资源类型（dynamic / lottery / rpa_action ...）")
    comments: list[CommentItem] = Field(
        default_factory=list,
        description="该类型最新根评论（只含根评论，不含楼中楼子评论）",
    )


class CommentLatestResp(SQLModel, AutoStrMixin):
    """首页最新评论（按资源类型分组展示）。"""

    groups: list[CommentLatestGroup] = Field(
        default_factory=list, description="各资源类型下的最新根评论分组，仅有数据的类型才会出现"
    )


# ==================== 互动 ====================


class CommentActionReq(SQLModel, AutoStrMixin):
    """点赞 / 点踩 / 取消。"""

    rpid: str = Field(description="评论id（字符串）")
    action: CommentActionEnum = Field(description="0取消 / 1点赞 / 2点踩")


class CommentActionResp(SQLModel, AutoStrMixin):
    rpid: str
    action: CommentActionEnum = CommentActionEnum.NONE
    like_count: int = 0
    hate_count: int = 0


# ==================== 置顶 / 管理 ====================


class CommentReportReq(SQLModel, AutoStrMixin):
    """举报评论。"""

    rpid: str = Field(description="被举报评论id（字符串）")
    reasonType: MomentReportReasonEnum = Field(description="举报原因类型（复用 MomentReportReasonEnum）")
    reasonDesc: str | None = Field(default=None, max_length=500, description="补充描述（选填）")


class CommentReportResp(SQLModel, AutoStrMixin):
    """举报评论响应。"""

    rpid: str = Field(description="被举报评论id（字符串）")
    reported: bool = Field(default=False, description="本次是否新增举报（True=首次，False=当日/重复已报）")
    switched_to_auditing: bool = Field(default=False, description="本次举报后是否已触发转审核（state→auditing）")


class CommentTopReq(SQLModel, AutoStrMixin):
    """置顶 / 取消置顶（内容作者或管理员）。"""

    oid: str = Field(description="业务实体id（字符串）")
    type: InteractionBizTypeEnum = Field(description="业务实体类型")
    rpid: str = Field(description="要置顶 / 取消置顶的评论id（字符串，必须是根评论）")
    top: bool = Field(default=True, description="True 置顶 / False 取消置顶")


class CommentTopResp(SQLModel, AutoStrMixin):
    top_rpid: str | None = Field(default=None, description="当前置顶评论id")
    success: bool = True


class CommentAuditReq(SQLModel, AutoStrMixin):
    """管理端人工审核 / 上下架。"""

    rpid: str = Field(description="待处理评论id（字符串）")
    # 通过 / 驳回 / 下架 / 恢复
    op: str = Field(description="pass | reject | hidden | restore")
    note: str | None = Field(default=None, max_length=256, description="审核备注")


class CommentBulkAuditReq(SQLModel, AutoStrMixin):
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


class CommentBulkAuditResp(SQLModel, AutoStrMixin):
    """批量审核结果汇总。"""

    total: int = Field(default=0, description="请求条数")
    success: int = Field(default=0, description="成功条数")
    failed: list[str] = Field(default_factory=list, description="失败的 rpid 列表")


class CommentAuditItem(SQLModel, AutoStrMixin, VisibilityMixin):
    """审核队列中的一条评论。

    原始 IP 属明文信息（决策 C3：出参打码、管理员明文），以 ``Private(admin_only=True)``
    标记，非管理员视角下由序列化器自动剥离。
    """

    rpid: str
    oid: str
    type: InteractionBizTypeEnum
    mid: int
    message: str
    state: ResourceAuditStatusEnum
    like_count: int = 0
    ctime: datetime
    ip_v4: Annotated[str | None, Private(admin_only=True)] = None
    ip_v6: Annotated[str | None, Private(admin_only=True)] = None
    plat: str | None = Field(default=None, max_length=32, description="来源平台")
    device: str | None = Field(default=None, max_length=64, description="来源设备")
    source: AuditSourceInfo | None = Field(
        default=None, description="内容来源，管理端可点击直达原始评论区"
    )
    # 管理端审核视角：可用**私有**简档（含脱敏邮箱 / 经验 / 大会员到期 / 角色），便于溯源
    member: UserBriefOut | None = Field(
        default=None, description="发布者信息（管理端：含私有字段），装配时直连 pptr 只读取回"
    )


class CommentAuditListResp(SQLModel, AutoStrMixin):
    items: list[CommentAuditItem] = Field(default_factory=list)
    total: int = 0
    page_num: int = 1
    page_size: int = 20
    states: list[ResourceAuditStatusEnum] = Field(
        default_factory=list, description="本次实际生效的状态过滤（非 root 恒为待审核）"
    )
    can_view_all_states: bool = Field(
        default=False, description="当前管理员是否可查看全部状态（仅 root 为 True）"
    )


class CommentSourceResp(SQLModel, AutoStrMixin):
    """一条评论的内容来源详情（管理端「内容来源」点击时按需拉取）。"""

    rpid: str
    root: str = "0"
    parent: str = "0"
    state: ResourceAuditStatusEnum = ResourceAuditStatusEnum.NORMAL
    source: AuditSourceInfo
    subject_state: CommentSubjectStateEnum | None = Field(
        default=None, description="所属评论区状态；评论区不存在时为 null"
    )
    root_count: int = Field(default=0, description="评论区一级评论数")
    all_count: int = Field(default=0, description="评论区评论总数（含楼中楼）")


class CommentAuditResp(SQLModel, AutoStrMixin):
    rpid: str
    state: ResourceAuditStatusEnum


class CommentStatsResp(SQLModel, AutoStrMixin):
    """评论区全局统计（管理端）。"""

    total_comments: int = 0
    total_root: int = 0
    total_subjects: int = 0
    today_new: int = 0
    # 管理端统计：同样使用**私有**简档
    top_authors: list[UserBriefOut] = Field(default_factory=list)
    # 各状态评论数（以 ResourceAuditStatusEnum.value 即 1~5 为整数键，
    # normal / auditing / rejected / hidden / deleted）；
    # total_comments 为该字典中「非 deleted」各项之和，确保驳回等状态被计入总数。
    state_counts: dict[int, int] = Field(default_factory=dict)


__all__ = [
    "CommentActionReq",
    "CommentActionResp",
    "CommentAddReq",
    "CommentAddResp",
    "CommentAuditItem",
    "CommentAuditListResp",
    "CommentAuditReq",
    "CommentAuditResp",
    "CommentCountResp",
    "CommentDelReq",
    "CommentItem",
    "CommentLatestGroup",
    "CommentLatestResp",
    "CommentListResp",
    "CommentOperationResp",
    "CommentReportReq",
    "CommentReportResp",
    "CommentSourceResp",
    "CommentStatsResp",
    "CommentSubListResp",
    "CommentUserBrief",
]
