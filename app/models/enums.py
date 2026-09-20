"""消息系统统一枚举定义。

所有业务枚举统一使用标准库 `enum.IntEnum` 整数枚举；落库经 `sqlalchemy.Enum(...)`
映射为 **MySQL 原生 ENUM**，库里存**成员名**（如 `'LOTTERY'`），而非整数字面量
（新增枚举值需要 DDL，这是原生 ENUM 的固有代价）。对外接口层（@auto_str / pydantic）
序列化时仍返回枚举的 `.value`（整数），与库里存成员名互不干扰。
"""
from enum import Enum

from bili_common.models import IntEnumAutoDoc
from bili_common.models.notify import NotifyLevelEnum, NotifyTargetTypeEnum
# 举报相关枚举（原因 / 审核状态）统一收口到 bili-common，be-message 侧直接复用，不再重定义：
from bili_common.models.report import ReportReasonEnum, ReportAuditStatusEnum


class MessageModuleEnum(IntEnumAutoDoc):
    """消息系统四大模块（对应 routing_key 的第二段）。"""

    PUSH = 1
    NOTIFY = 2
    EVENT = 3
    DM = 4


# ==================== 系统通知 ====================


class NotifyStatusEnum(IntEnumAutoDoc):
    """系统通知的生命周期状态。"""

    # 草稿：管理员已创建但未发布，不会被任何用户拉取到
    DRAFT = 1
    # 已发布：到达 publish_at 后可被拉取
    PUBLISHED = 2
    # 已撤回：管理员撤回，用户侧立即不可见
    REVOKED = 3


# ==================== 事件提醒 ====================
# 互动操作类型已收口到 bili-common 的 `InteractionActionTypeEnum`（动作维度，与
# `InteractionBizTypeEnum` 资源维度明确区分），直接 `from bili_common.models import ...`
# 使用，本模块不 re-export，详见 bili-common。

# ==================== 私信 ====================


class DmMsgTypeEnum(IntEnumAutoDoc):
    """私信消息类型。"""

    TEXT = 1
    IMAGE = 2
    SYSTEM = 3


class DmMsgStatusEnum(IntEnumAutoDoc):
    """私信消息在某个用户视角下的状态（写扩散，双方各自独立）。"""

    # 正常可见
    NORMAL = 0
    # 已撤回：双方均不可见正文，展示「消息已撤回」占位
    RECALLED = 1
    # 已删除：仅删除者本人不可见，对方不受影响
    DELETED = 2


class DmSessionTypeEnum(IntEnumAutoDoc):
    """会话类型。

    - `SINGLE`：1 对 1 私信（普通 DM）。陌生人但被接收方接受的会话也归此类。
    - `STRANGER`：陌生人私信分类（参考 B 站「陌生人消息」集合）。
      仅在接收方开启「陌生人私信拦截」且发送方为陌生人时产生 —— 消息会落
      接收方的 STRANGER 收件箱，而**不会**进 SINGLE 主列表；前端在会话列
      表顶部以聚合条目展示，点击后进入专门的陌生人子列表。
    """

    SINGLE = 1
    STRANGER = 2


class DmRelationEnum(IntEnumAutoDoc):
    """会话双方关系，用于陌生人私信过滤。"""

    # 普通会话：对方主动发起过或已被接收方回复
    NORMAL = 1
    # 陌生人会话：接收方从未回复过，落入「陌生人消息」分组
    STRANGER = 2


# ==================== 评论系统 ====================


class CommentSubjectStateEnum(IntEnumAutoDoc):
    """评论区状态。"""

    # 正常，可读可写
    NORMAL = 1
    # 已关闭：只读，不接受新评论
    CLOSED = 2


class CommentActionEnum(IntEnumAutoDoc):
    """评论互动动作（点赞 / 点踩）。

    与原 Node 端 `TCommentInteractRelation.action` 的语义保持一致，
    便于前端复用同一套取值。
    """

    # 无态（等价于取消点赞 / 取消点踩）
    NONE = 0
    LIKE = 1
    HATE = 2


class CommentSortEnum(IntEnumAutoDoc):
    """评论列表排序方式。"""

    # 热度排序：读冗余列 hot_score，走 idx_comment_hot
    HOT = 1
    # 时间排序：rpid 单调递增，等价于按发布时间
    TIME = 2


class CommentAttrBit(IntEnumAutoDoc):
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


class BanServiceEnum(IntEnumAutoDoc):
    """可被封禁的服务范围，与评论 / 私信审核一一对应。

    封禁记录按服务维度隔离：封评论只影响评论区，不影响私信。
    """

    COMMENT = 1
    DM = 2


class BanDurationTypeEnum(IntEnumAutoDoc):
    """封禁时长类型。

    - `TEMPORARY`(1)：限时封禁，配合 `duration_days` 计算解封时间；
    - `PERMANENT`(2)：永久封禁，无到期时间。
    """

    TEMPORARY = 1
    PERMANENT = 2


class BanStatusEnum(IntEnumAutoDoc):
    """封禁记录的生命周期状态。

    - `ACTIVE`(1)：生效中（限时封禁到期自动由读取层判定为失效，无需定时任务翻转）；
    - `LIFTED`(2)：已被管理员手动解封。
    """

    ACTIVE = 1
    LIFTED = 2


# ==================== 用户经验 ====================


class ExpActionType(IntEnumAutoDoc):
    """用户经验增加行为类型，数据库存 int，对外接口转 string。"""

    DAILY_LOGIN = 1
    # 后续扩展其他行为：POST_COMMENT = 2, SHARE_VIDEO = 3, 等


# ==================== 用户动态（动态卡片模块）====================


class MomentTypeEnum(IntEnumAutoDoc):
    """Moment 类型（原"动态"，对齐 B站 MomentType，概念重命名为 Moment）。

    MVP 仅支持 WORD / FORWARD，其余类型后续迭代补充。
    数据库存 int，对外接口转 string 名称。
    """

    FORWARD = 1
    WORD = 6


class MomentVisibleScopeEnum(IntEnumAutoDoc):
    """Moment 可见范围。"""

    # 公开
    PUBLIC = 0
    # 仅关注的人
    FOLLOWER = 1
    # 仅自己
    SELF = 2
    # 充电专享
    CHARGE = 3


class MomentFoldTypeEnum(IntEnumAutoDoc):
    """Moment 折叠类型。"""

    NONE = 0
    USER_FOLD = 1
    OVER_FREQ_FOLD = 2


# 举报原因类型直接复用 bili-common 的 `ReportReasonEnum`（统一举报原因：1-6 与历史
# MomentReportReasonEnum 取值一致，且额外含 AD/FLAME 等）。不再单独定义，避免与通用
# 举报枚举取值漂移。
MomentReportReasonEnum = ReportReasonEnum

# 举报审核状态直接复用 bili-common 的 `ReportAuditStatusEnum`（PENDING/RESOLVED/REJECTED
# 取值一致）。不再单独定义。
MomentReportAuditStatusEnum = ReportAuditStatusEnum


class MomentAuditLogActionEnum(IntEnumAutoDoc):
    """通用资源审核流转动作类型（2.55.0 起写 `TResourceAuditLog.actionType`；原 `TMomentAuditLog`）。"""

    CREATE = 1
    EDIT = 2
    APPROVE = 3
    REJECT = 4
    RESUBMIT = 5
    DELETE = 6


class MomentAuditLogOperatorRoleEnum(IntEnumAutoDoc):
    """Moment 审核流转操作人角色。"""

    AUTHOR = 1
    ADMIN = 2


class ResourceAuditStatusEnum(IntEnumAutoDoc):
    """统一审核生命周期状态（`NORMAL=1` 基准，实体 / 单据可共同表达）。

    阶段二起为 be-message 各资源审核态列的**唯一枚举**（原 `MomentAuditStatusEnum` /
    `MomentTopicAuditStatusEnum` / `CommentStateEnum` / `DmAuditStateEnum` /
    `AvatarAuditStatusEnum` / `FolderCoverAuditStatusEnum` 均收敛于此）：

    - `NORMAL`(1)   ：通过 / 公开 / 已批准（可入 Feed、可互动；单据=已通过）；
    - `AUDITING`(2) ：待审（实体先发后审；头像/封面等单据的 PENDING 亦映射本值）；
    - `REJECTED`(3) ：驳回（实体 / 单据驳回）；
    - `HIDDEN`(4)   ：管理员下架（实体类）；
    - `DELETED`(5)  ：软删（当前仅评论表达；动态软删走独立 `deletedAt` 列）。

    举报单（`ReportAuditStatusEnum`）独立，不并入本枚举。
    """

    NORMAL = 1
    AUDITING = 2
    REJECTED = 3
    HIDDEN = 4
    DELETED = 5


# 互动资源类型枚举（`InteractionBizTypeEnum`）与互动操作枚举（`InteractionActionTypeEnum`）
# 已从本文件移除（不再 re-export），使用方直接从 `bili_common.models` 导入；
# 举报枚举（`ReportReasonEnum` / `ReportAuditStatusEnum`）同样收口在 bili-common，
# 本文件仅做 be-message 侧别名复用 / 定义 be-message 专属枚举。


# ==================== 用户关注关系 ====================


class FollowStatusEnum(IntEnumAutoDoc):
    """用户间关系状态（关注 / 拉黑），按方向独立记录。

    - `FOLLOWING`(1)：mid 主动关注 target_mid；
    - `BLOCKED`(2) ：mid 拉黑 target_mid，target_mid 不能关注 / 私信 mid。

    一条记录只代表「mid → target_mid」单一方向的关系，互相关注需要
    两条 `following` 记录（双向各一）。`uq(mid, target_mid)` 保证
    同一方向只有一条生效记录。
    """

    FOLLOWING = 1
    BLOCKED = 2


# ==================== 前端路由名（跳转契约）====================


class FrontendRouteEnum(str, Enum):
    """前端路由名（跳转契约的唯一真相源，见计划书 §2.10）。

    **值必须等于前端 `Vue3FrontEndDemoExercise/src/router/index.ts` 里路由的 `name`**
    （部分路由的 name 取自 `src/models/router/index.ts::RouteName` 枚举，其值为中文，此处照抄）。

    后端下发跳转目标时**只给路由名**（`route:{name}?{query}`），不写任何站内路径，
    因此路径只在前端路由表里存在一份，前端改路径不必同步后端。

    新增跳转点：先在这里加成员 → 前端加/改对应路由，两侧 name 对齐即可。
    """

    # 抽奖卡片详情（RouteName.LOTTERY_CARD_DETAIL）→ /app/lot-data/card-detail
    LOTTERY_CARD_DETAIL = "抽奖卡片详情"
    # 第三方抽奖动态详情（RouteName.OTHERS_LOT_DYN_DETAIL）→ /app/lot-data/others-dyn-detail?dynId=
    # 2.61.0：第三方抽奖动态没有 lottery_id，不能复用抽奖卡片详情页（那按 lottery_id 拉详情）
    OTHERS_LOT_DYN_DETAIL = "第三方抽奖动态详情"
    # 动态详情 → /app/moment/detail/:momentId
    MOMENT_DETAIL = "MOMENT_DETAIL"
    # 私信审核（管理端）→ /app/admin/message-dm
    ADMIN_MESSAGE_DM = "ADMIN_MESSAGE_DM"

    @classmethod
    def from_name(cls, name: str) -> "FrontendRouteEnum":
        """按路由名取成员，未知名抛 ``ValueError``（供校验兜底使用）。"""
        return cls(name)


__all__ = [
    "AvatarAuditStatusEnum",
    "FrontendRouteEnum",
    "FolderCoverAuditStatusEnum",
    "BanDurationTypeEnum",
    "BanServiceEnum",
    "BanStatusEnum",
    "CommentActionEnum",
    "CommentAttrBit",
    "CommentSortEnum",
    "CommentStateEnum",
    "CommentSubjectStateEnum",
    "DmAuditStateEnum",
    "DmMsgStatusEnum",
    "DmMsgTypeEnum",
    "DmRelationEnum",
    "DmSessionTypeEnum",
    "ExpActionType",
    "FollowStatusEnum",
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
]
