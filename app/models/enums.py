"""消息系统统一枚举定义。

所有字符串枚举一律使用 `StrEnum`，落库时按字符串存储（VARCHAR），
既保留可读性，也避免 MySQL ENUM 类型变更需要 DDL 的问题。
"""

from enum import IntEnum, StrEnum


class MessageModuleEnum(StrEnum):
    """消息系统四大模块（对应 routing_key 的第二段）。"""

    PUSH = "push"
    NOTIFY = "notify"
    EVENT = "event"
    DM = "dm"


# ==================== 系统通知 ====================


class NotifyTargetTypeEnum(StrEnum):
    """系统通知的目标用户类型（按用户类型推送）。"""

    # 全体用户
    ALL = "all"
    # 按角色：target_value 为 root / normal
    ROLE = "role"
    # 按等级：target_value 为最低等级，用户 level >= 该值即命中
    LEVEL = "level"
    # 仅大会员：命中 vip_status 非空且不为 "0"
    VIP = "vip"
    # 指定用户：target_value 为逗号分隔的 mid 列表
    CUSTOM = "custom"


class NotifyStatusEnum(StrEnum):
    """系统通知的生命周期状态。"""

    # 草稿：管理员已创建但未发布，不会被任何用户拉取到
    DRAFT = "draft"
    # 已发布：到达 publish_at 后可被拉取
    PUBLISHED = "published"
    # 已撤回：管理员撤回，用户侧立即不可见
    REVOKED = "revoked"


class NotifyLevelEnum(StrEnum):
    """通知重要级别，决定推送策略的激进程度。"""

    NORMAL = "normal"
    IMPORTANT = "important"
    URGENT = "urgent"


# ==================== 事件提醒 ====================


class EventTypeEnum(StrEnum):
    """用户行为事件类型（点赞 / 回复 / @提及 / 审核驳回）。"""

    LIKE = "like"
    REPLY = "reply"
    AT = "at"
    AUDIT_REJECT = "audit_reject"


class SourceTypeEnum(StrEnum):
    """事件来源实体类型，与 source_id 共同构成聚合分组键。"""

    VIDEO = "video"
    DYNAMIC = "dynamic"
    ARTICLE = "article"
    COMMENT = "comment"
    LOTTERY = "lottery"
    OTHER = "other"


# ==================== 私信 ====================


class DmMsgTypeEnum(StrEnum):
    """私信消息类型。"""

    TEXT = "text"
    IMAGE = "image"
    SYSTEM = "system"


class DmMsgStatusEnum(IntEnum):
    """私信消息在某个用户视角下的状态（写扩散，双方各自独立）。"""

    # 正常可见
    NORMAL = 0
    # 已撤回：双方均不可见正文，展示「消息已撤回」占位
    RECALLED = 1
    # 已删除：仅删除者本人不可见，对方不受影响
    DELETED = 2


class DmSessionTypeEnum(IntEnum):
    """会话类型，预留群聊扩展。"""

    SINGLE = 1


class DmRelationEnum(StrEnum):
    """会话双方关系，用于陌生人私信过滤。"""

    # 普通会话：对方主动发起过或已被接收方回复
    NORMAL = "normal"
    # 陌生人会话：接收方从未回复过，落入「陌生人消息」分组
    STRANGER = "stranger"


class DmAuditStateEnum(StrEnum):
    """私信管理端审核状态（与评论审核对齐）。

    可见性规则：
    - `normal`   ：正常可见；
    - `auditing` ：待审核（先发后审，作者无感知）；
    - `rejected` / `hidden`：对用户不可见（聊天窗过滤，列表不返回）。
    """

    NORMAL = "normal"
    AUDITING = "auditing"
    REJECTED = "rejected"
    HIDDEN = "hidden"


# ==================== 评论系统 ====================


class CommentTypeEnum(StrEnum):
    """评论区所属的业务实体类型，与 oid 共同唯一定位一个评论区。"""

    # 用户动态
    DYNAMIC = "dynamic"
    # 专栏 / 图文
    ARTICLE = "article"
    # 抽奖活动
    LOTTERY = "lottery"
    # 站内反馈（承接原 Node 端 feedback 场景）
    FEEDBACK = "feedback"
    OTHER = "other"


class CommentSubjectStateEnum(StrEnum):
    """评论区状态。"""

    # 正常，可读可写
    NORMAL = "normal"
    # 已关闭：只读，不接受新评论
    CLOSED = "closed"


class CommentStateEnum(StrEnum):
    """单条评论的生命周期状态。

    可见性规则（Phase 5 审核落地后完整生效）：

    - `normal`   ：所有人可见
    - `auditing` ：仅作者本人可见（对齐 B 站「先发后审」，作者无感知）
    - `rejected` / `hidden` / `deleted`：列表不返回
    """

    NORMAL = "normal"
    # 待审核：命中疑似敏感词，等待人工 / AI 复审
    AUDITING = "auditing"
    # 审核驳回
    REJECTED = "rejected"
    # 管理员下架
    HIDDEN = "hidden"
    # 用户 / 管理员删除（软删）
    DELETED = "deleted"


class CommentActionEnum(IntEnum):
    """评论互动动作（点赞 / 点踩）。

    与原 Node 端 `TCommentInteractRelation.action` 的语义保持一致，
    便于前端复用同一套取值。
    """

    # 无态（等价于取消点赞 / 取消点踩）
    NONE = 0
    LIKE = 1
    HATE = 2


class CommentSortEnum(StrEnum):
    """评论列表排序方式。"""

    # 热度排序：读冗余列 hot_score，走 idx_comment_hot
    HOT = "hot"
    # 时间排序：rpid 单调递增，等价于按发布时间
    TIME = "time"


class CommentAttrBit(IntEnum):
    """`msg_comment_index.attr` 位图标记。

    用位图而不是多个 bool 列：新增标记不需要 DDL 改表，
    且单次读取即可拿到全部标记状态。
    """

    # 置顶（与 msg_comment_subject.top_rpid 同步维护）
    TOP = 1
    # 精选
    ESSENCE = 2
    # UP 主点过赞
    UP_LIKED = 4


# ==================== 用户封禁（审核联动）====================


class BanServiceEnum(StrEnum):
    """可被封禁的服务范围，与评论 / 私信审核一一对应。

    封禁记录按服务维度隔离：封评论只影响评论区，不影响私信。
    """

    COMMENT = "comment"
    DM = "dm"


class BanDurationTypeEnum(StrEnum):
    """封禁时长类型。

    - `temporary`：限时封禁，配合 `duration_days` 计算解封时间；
    - `permanent`：永久封禁，无到期时间。
    """

    TEMPORARY = "temporary"
    PERMANENT = "permanent"


class BanStatusEnum(StrEnum):
    """封禁记录的生命周期状态。

    - `active`：生效中（限时封禁到期自动由读取层判定为失效，无需定时任务翻转）；
    - `lifted`：已被管理员手动解封。
    """

    ACTIVE = "active"
    LIFTED = "lifted"


# ==================== 用户经验 ====================


class ExpActionType(IntEnum):
    """用户经验增加行为类型，数据库存 int，对外接口转 string。"""

    DAILY_LOGIN = 1
    # 后续扩展其他行为：POST_COMMENT = 2, SHARE_VIDEO = 3, 等


# ==================== 用户动态（动态卡片模块）====================


class MomentTypeEnum(IntEnum):
    """Moment 类型（原"动态"，对齐 B站 MomentType，概念重命名为 Moment）。

    MVP 仅支持 WORD / FORWARD，其余类型后续迭代补充。
    数据库存 int，对外接口转 string 名称。
    """

    FORWARD = 1
    WORD = 6


class MomentAuditStatusEnum(StrEnum):
    """Moment 审核生命周期状态。

    - `auditing`：审核中（先发后审，作者本人空间可见，普通用户不可见）；
    - `normal`  ：审核通过，进入 Feed 流全量可见；
    - `rejected`：审核驳回，作者可编辑后重新提交或删除；
    - `hidden`  ：管理员下架。
    """

    AUDITING = "auditing"
    NORMAL = "normal"
    REJECTED = "rejected"
    HIDDEN = "hidden"


class MomentTopicAuditStatusEnum(StrEnum):
    """话题审核生命周期状态（TMomentTopic.auditStatus，对齐动态审核）。

    - `auditing`：待审核（用户创建，不公开展示）；
    - `normal`  ：审核通过，进入话题广场 / Feed / 热搜；
    - `rejected`：审核驳回，仅创建者「我的话题」可见（含驳回原因）。
    """

    AUDITING = "auditing"
    NORMAL = "normal"
    REJECTED = "rejected"


class MomentVisibleScopeEnum(IntEnum):
    """Moment 可见范围。"""

    # 公开
    PUBLIC = 0
    # 仅关注的人
    FOLLOWER = 1
    # 仅自己
    SELF = 2
    # 充电专享
    CHARGE = 3


class MomentFoldTypeEnum(IntEnum):
    """Moment 折叠类型。"""

    NONE = 0
    USER_FOLD = 1
    OVER_FREQ_FOLD = 2


class MomentReportReasonEnum(IntEnum):
    """Moment 举报原因类型。"""

    # 不实信息
    FAKE_INFO = 1
    # 违法违规
    ILLEGAL = 2
    # 人身攻击
    PERSONAL_ATTACK = 3
    # 色情低俗
    PORN = 4
    # 诈骗
    FRAUD = 5
    # 其他
    OTHER = 6


class MomentReportAuditStatusEnum(StrEnum):
    """Moment 举报处理状态。"""

    PENDING = "pending"
    RESOLVED = "resolved"
    REJECTED = "rejected"


class MomentAuditLogActionEnum(StrEnum):
    """Moment 审核流转动作类型（写 TMomentAuditLog.actionType）。"""

    CREATE = "create"
    EDIT = "edit"
    APPROVE = "approve"
    REJECT = "reject"
    RESUBMIT = "resubmit"
    DELETE = "delete"


class MomentAuditLogOperatorRoleEnum(StrEnum):
    """Moment 审核流转操作人角色。"""

    AUTHOR = "author"
    ADMIN = "admin"


# 互动资源类型枚举统一收口到 bili-common（2.18.0 去重），此处 re-export 保持兼容
from bili_common.models.interaction import InteractionBizTypeEnum  # noqa: E402


# ==================== 用户关注关系 ====================


class FollowStatusEnum(StrEnum):
    """用户间关系状态（关注 / 拉黑），按方向独立记录。

    - `following`：mid 主动关注 target_mid；
    - `blocked` ：mid 拉黑 target_mid，target_mid 不能关注 / 私信 mid。

    一条记录只代表「mid → target_mid」单一方向的关系，互相关注需要
    两条 `following` 记录（双向各一）。`uq(mid, target_mid)` 保证
    同一方向只有一条生效记录。
    """

    FOLLOWING = "following"
    BLOCKED = "blocked"


class AvatarAuditStatusEnum(StrEnum):
    """头像更换审核状态（TUserAvatarAudit.auditStatus）。

    - `pending`：待审核，未对外展示；
    - `approved`：审核通过，newAvatar 已写入 TUserDetail.avatar 公开显示；
    - `rejected`：审核驳回，保持原头像。
    """

    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


__all__ = [
    "AvatarAuditStatusEnum",
    "BanDurationTypeEnum",
    "BanServiceEnum",
    "BanStatusEnum",
    "CommentActionEnum",
    "CommentAttrBit",
    "CommentSortEnum",
    "CommentStateEnum",
    "CommentSubjectStateEnum",
    "CommentTypeEnum",
    "DmAuditStateEnum",
    "DmMsgStatusEnum",
    "DmMsgTypeEnum",
    "DmRelationEnum",
    "DmSessionTypeEnum",
    "EventTypeEnum",
    "ExpActionType",
    "FollowStatusEnum",
    "InteractionBizTypeEnum",
    "MessageModuleEnum",
    "MomentAuditLogActionEnum",
    "MomentAuditLogOperatorRoleEnum",
    "MomentAuditStatusEnum",
    "MomentFoldTypeEnum",
    "MomentReportAuditStatusEnum",
    "MomentReportReasonEnum",
    "MomentTypeEnum",
    "MomentVisibleScopeEnum",
    "NotifyLevelEnum",
    "NotifyStatusEnum",
    "NotifyTargetTypeEnum",
    "SourceTypeEnum",
]
