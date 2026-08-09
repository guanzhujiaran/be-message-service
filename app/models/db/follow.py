"""用户关注 / 拉黑关系表模型。

`msg_user_follow` 自包含于 be-message 的 MySQL 主库，**不回写 pptr**：
用户主数据（uid / uname / 头像 / 等级 / 大会员）只有一份，在 pptr 的
Postgres，渲染关注列表时由 `PptrUserService` 直连只读批量回查（一次
`WHERE uid IN (...)`），既避免 N+1，也不重复保存用户数据。

设计要点：

- 一条记录代表「mid → target_mid」单一方向的关系，`status` 区分
  `following`（关注）/ `blocked`（拉黑）；互相关注需要两条 `following`
  记录（双向各一）。
- `uq(mid, target_mid)` 保证同一方向只有一条生效记录：重复关注 / 重复
  拉黑由数据库兜底，业务层只需 upsert 即可。
- 索引：

  - `idx_follow_mid_status`：按 mid + 状态筛「我关注的人 / 我拉黑的人」；
  - `idx_follow_target_status`：按 target_mid + 状态筛「我的粉丝」；
  - `uq(mid, target_mid)`：等值点查「我对某人的当前关系」最高频，自带索引。

- 拉黑语义：A 拉黑 B 时，A → B 的记录变为 `blocked`；若 B 之前关注过 A
  （B → A 的 `following` 记录）由业务层负责删除，使 B 不再是 A 的粉丝。
  拉黑期间 B 尝试关注 / 私信 A 将被业务层拒绝。
"""

from sqlalchemy import BIGINT, Index, UniqueConstraint
from sqlmodel import Field, SQLModel

from app.models.db.base import TimestampMixin, str_enum_type
from app.models.enums import FollowStatusEnum


class UserFollow(TimestampMixin, table=True):
    """用户关注 / 拉黑关系（按方向独立记录）。"""

    __tablename__ = "msg_user_follow"
    __table_args__ = (
        # 同一方向只允许一条生效记录（关注或拉黑二选一）
        UniqueConstraint("mid", "target_mid", name="uq_user_follow_mid_target"),
        # 我关注的人 / 我拉黑的人
        Index("idx_follow_mid_status", "mid", "status", "created_at"),
        # 我的粉丝（被关注）/ 拉黑我的人（一般不展示，仅内部判定）
        Index("idx_follow_target_status", "target_mid", "status", "created_at"),
        {"extend_existing": True},
    )

    id: int | None = Field(default=None, primary_key=True)

    # 主动方 mid：发起关注 / 拉黑的用户
    mid: int = Field(sa_type=BIGINT, index=True, description="主动方 mid")
    # 被动方 mid：被关注 / 被拉黑的用户
    target_mid: int = Field(
        sa_type=BIGINT, index=True, description="被动方 mid"
    )

    # 关系状态：following 关注 / blocked 拉黑
    status: FollowStatusEnum = Field(
        default=FollowStatusEnum.FOLLOWING,
        sa_type=str_enum_type(FollowStatusEnum),
        index=True,
        description="关系状态：following 关注 / blocked 拉黑",
    )


__all__ = ["UserFollow", "SQLModel"]
