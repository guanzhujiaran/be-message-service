"""评论系统相关表模型。

存储分层对齐 B 站评论系统的「三表设计」（评论区 / 评论索引 / 评论内容）：

- `msg_comment_subject`：评论区维度的元信息与计数，(oid, type) 唯一定位。
- `msg_comment_index`  ：评论索引，**列表查询只扫这张表**，字段全部轻量定长，
  排序 / 分页 / 过滤所需的一切（计数、热度、状态、位图标记）都冗余在此。
- `msg_comment_content`：评论正文与图片等大字段，与索引表 1:1。
  拆出去的唯一目的是让列表查询不被 TEXT / JSON 撑大的行拖慢 —— 索引表
  单行越小，同一个数据页装下的行越多，翻页扫描的 IO 就越少。

配套两张关系表：

- `msg_comment_action`：点赞 / 点踩，`uq(rpid, mid)` 让「重复点赞」由数据库兜底。
- `msg_comment_at`    ：@提及关系，用于通知投递与补偿重试。

用户展示信息（昵称 / 头像 / 等级 / 大会员等）**不在本库冗余**：用户主数据只有一份，
在 pptr 的 Postgres（TUserInfo / TUserDetail / TUserVip / TUserLevel）。渲染评论列表时
由 `PptrUserService` 直连该库**只读**地批量取回（一次 `WHERE uid IN (...)`），
既避免 N+1，也不重复保存用户数据。

**rpid 是雪花 ID（复用 app.core.sharding 的生成器）**：单调递增，因此
「按 rpid 倒序」等价于「按发布时间倒序」，时间排序不需要额外的时间索引。
64 位整数一律以字符串出参，避免浏览器 `Number.MAX_SAFE_INTEGER` 精度丢失。
"""

from sqlalchemy import BIGINT, JSON, Text
from sqlmodel import Column, Field, Index, SQLModel, UniqueConstraint

from app.models.db.base import TimestampMixin, int_enum_type, str_enum_type
from app.models.enums import (
    CommentActionEnum,
    CommentStateEnum,
    CommentSubjectStateEnum,
    CommentTypeEnum,
)


class CommentSubject(TimestampMixin, table=True):
    """评论区（一个业务实体对应一个评论区）。"""

    __tablename__ = "msg_comment_subject"
    __table_args__ = (
        UniqueConstraint("oid", "type", name="uq_comment_subject_oid_type"),
        {"extend_existing": True},
    )

    id: int | None = Field(default=None, primary_key=True)

    oid: int = Field(sa_type=BIGINT, index=True, description="业务实体id")
    type: CommentTypeEnum = Field(
        sa_type=str_enum_type(CommentTypeEnum),
        description="业务实体类型，与 oid 共同唯一定位评论区",
    )

    up_mid: int = Field(
        default=0,
        sa_type=BIGINT,
        index=True,
        description="内容作者mid：决定谁有权置顶 / 删除他人评论",
    )

    # ---- 冗余计数：列表接口一律读这里，禁止 COUNT(*) 扫描 ----
    root_count: int = Field(default=0, description="一级评论数（不含楼中楼）")
    all_count: int = Field(default=0, description="评论总数（含楼中楼）")

    # ---- 楼层号发号：数据库侧原子自增，保证同一评论区楼层连续不重号 ----
    # 对应 B 站「第 N 楼」展示；由发布事务内 `UPDATE ... SET floor_seq = floor_seq + 1`
    # 串行推进，避免并发跳号 / 重号（替代 Phase 2.4 设想的 MQ 串行发号，正确性等价且更简单）。
    floor_seq: int = Field(default=0, description="楼层发号计数器，一级评论发表时 +1")

    state: CommentSubjectStateEnum = Field(
        default=CommentSubjectStateEnum.NORMAL,
        sa_type=str_enum_type(CommentSubjectStateEnum),
        index=True,
        description="评论区状态：normal 可评论 / closed 只读",
    )

    top_rpid: int | None = Field(
        default=None,
        sa_type=BIGINT,
        description="置顶评论rpid，全区唯一一条；与 index.attr 的 TOP 位同步维护",
    )


class CommentIndex(TimestampMixin, table=True):
    """评论索引（列表查询主表，字段保持轻量）。"""

    __tablename__ = "msg_comment_index"
    __table_args__ = (
        # 热度排序：等值段 (oid,type,root,state) + 排序段 (hot_score,rpid)，
        # 排序直接吃冗余列，因此接口层**禁止** ORDER BY 表达式（会退化成 filesort）
        Index(
            "idx_comment_hot", "oid", "type", "root", "state", "hot_score", "rpid"
        ),
        # 时间排序：rpid 单调递增，等价于按 ctime
        Index("idx_comment_time", "oid", "type", "root", "state", "rpid"),
        # 楼中楼批量拉取：WHERE root IN (...) AND state=... ORDER BY rpid
        Index("idx_comment_sub", "root", "state", "rpid"),
        # 用户维度：个人评论列表 / 数据统计
        Index("idx_comment_user", "mid", "rpid"),
        {"extend_existing": True},
    )

    rpid: int = Field(
        sa_column=Column(BIGINT, primary_key=True, autoincrement=False),
        description="评论id（雪花ID，出参为字符串）",
    )

    oid: int = Field(sa_type=BIGINT, index=True, description="所属评论区的业务实体id")
    type: CommentTypeEnum = Field(
        sa_type=str_enum_type(CommentTypeEnum), description="所属评论区类型"
    )
    mid: int = Field(sa_type=BIGINT, index=True, description="评论发布者mid")

    # ---- 树形关系（对齐 B 站语义）----
    root: int = Field(
        default=0, sa_type=BIGINT, index=True, description="根评论rpid；0 表示自己就是一级评论"
    )
    parent: int = Field(
        default=0, sa_type=BIGINT, description="直接父评论rpid；0 表示一级评论"
    )
    dialog: int = Field(
        default=0,
        sa_type=BIGINT,
        index=True,
        description="同一楼中楼会话串id，用于「只看该对话」；一级评论为0",
    )
    reply_to_mid: int = Field(
        default=0, sa_type=BIGINT, description="被回复者mid，楼中楼展示「回复 @xxx」"
    )
    floor: int = Field(default=0, description="楼层号，由 MQ 串行发号，0 表示尚未发号")

    # ---- 冗余计数与热度 ----
    like_count: int = Field(default=0, description="点赞数")
    hate_count: int = Field(default=0, description="点踩数")
    rcount: int = Field(default=0, description="子评论数（仅一级评论维护）")
    hot_score: float = Field(
        default=0.0,
        index=True,
        description="热度分（冗余列）：like - hate*1.5 + rcount*0.5 + 时间衰减",
    )

    state: CommentStateEnum = Field(
        default=CommentStateEnum.NORMAL,
        sa_type=str_enum_type(CommentStateEnum),
        index=True,
        description="评论状态，决定可见性",
    )
    attr: int = Field(
        default=0, description="位图标记：1置顶 / 2精选 / 4UP主赞过（见 CommentAttrBit）"
    )


class CommentContent(TimestampMixin, table=True):
    """评论正文（与索引表 1:1，承载全部大字段）。"""

    __tablename__ = "msg_comment_content"
    __table_args__ = ({"extend_existing": True},)

    rpid: int = Field(
        sa_column=Column(BIGINT, primary_key=True, autoincrement=False),
        description="评论id，与 msg_comment_index.rpid 一一对应",
    )

    message: str = Field(
        sa_column=Column(Text, nullable=False),
        description="评论正文；@提及以 @{mid} 占位符存储，渲染期再展开为昵称链接",
    )
    pictures: list[str] = Field(
        default_factory=list,
        sa_column=Column(JSON, nullable=False),
        description="图片URL数组，最多9张。只存URL，服务端不做转存",
    )
    at_mids: list[int] = Field(
        default_factory=list,
        sa_column=Column(JSON, nullable=False),
        description="被@用户的mid列表，与 msg_comment_at 冗余，供渲染期批量取昵称",
    )
    emote_meta: dict | None = Field(
        default=None,
        sa_column=Column(JSON, nullable=True),
        description="表情包元信息（表情名 → 图片URL等）",
    )

    # ---- IP：只存原始地址，不做属地解析；出参必须打码（见 app.utils.ip_mask）----
    ip_v4: str | None = Field(
        default=None, max_length=15, description="原始IPv4，明文仅管理员可见"
    )
    ip_v6: str | None = Field(
        default=None, max_length=45, description="原始IPv6，明文仅管理员可见"
    )

    plat: str | None = Field(default=None, max_length=32, description="来源平台")
    device: str | None = Field(default=None, max_length=64, description="来源设备")


class CommentAction(TimestampMixin, table=True):
    """评论点赞 / 点踩关系。

    `uq(rpid, mid)` 是「限制重复点赞」的根本保障：
    并发下的重复请求由数据库唯一约束直接拦掉，业务层无需自旋加锁。
    """

    __tablename__ = "msg_comment_action"
    __table_args__ = (
        UniqueConstraint("rpid", "mid", name="uq_comment_action_rpid_mid"),
        {"extend_existing": True},
    )

    id: int | None = Field(default=None, primary_key=True)

    rpid: int = Field(sa_type=BIGINT, index=True, description="评论id")
    mid: int = Field(sa_type=BIGINT, index=True, description="操作者mid")
    action: CommentActionEnum = Field(
        default=CommentActionEnum.NONE,
        sa_type=int_enum_type(CommentActionEnum),
        index=True,
        description="0无 / 1已点赞 / 2已点踩",
    )


class CommentAt(TimestampMixin, table=True):
    """评论中的 @提及关系。

    正文里已冗余 `at_mids`，这里额外落一张关系表的原因有二：
    1. `uq(rpid, at_mid)` 保证同一条评论对同一人只通知一次（幂等）；
    2. `notified` 标记让「通知投递失败」可被定时任务扫出来补偿重试。
    """

    __tablename__ = "msg_comment_at"
    __table_args__ = (
        UniqueConstraint("rpid", "at_mid", name="uq_comment_at_rpid_at_mid"),
        # 补偿扫描：捞出尚未投递通知的记录
        Index("idx_comment_at_pending", "notified", "id"),
        {"extend_existing": True},
    )

    id: int | None = Field(default=None, primary_key=True)

    rpid: int = Field(sa_type=BIGINT, index=True, description="评论id")
    oid: int = Field(sa_type=BIGINT, description="所属评论区的业务实体id")
    type: CommentTypeEnum = Field(
        sa_type=str_enum_type(CommentTypeEnum), description="所属评论区类型"
    )
    from_mid: int = Field(sa_type=BIGINT, description="发起@的用户mid")
    at_mid: int = Field(sa_type=BIGINT, index=True, description="被@的用户mid")
    notified: bool = Field(default=False, description="是否已投递@通知")


__all__ = [
    "CommentSubject",
    "CommentIndex",
    "CommentContent",
    "CommentAction",
    "CommentAt",
    "SQLModel",
]
