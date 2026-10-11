"""关注分组（tag）表模型，对标 B 站 `x/relation/tags`。

对齐 `app/models.db.follow_tbl` 的既有风格（`msg_` 前缀表名 + snake_case 列名）：

设计要点：
- **两张表**：`msg_user_follow_tag`（分组定义）+ `msg_user_follow_tag_user`（分组-成员关系）。
  刻意**不给 `msg_user_follow` 加列**：分组是可解耦的附属标签，取关 / 拉黑时只需删关系行，
  关注主表结构不受影响。
- **系统分组不落库**：`tag_id = -10`（特别关注）与「默认分组」是约定值，
  由服务层固定返回（`is_system=1`）；默认分组 = 当前关注里**没有落到任何分组**的那些人，
  所以它的 count = 关注总数 − 各分组 count 之和，不需要单独存关系。
- `uq(mid, name)`：同一用户分组名不重复（大小写敏感，MySQL 默认 collation）。
- `uq(tag_id, target_mid)`：同一用户在同一分组里只出现一次。
- 索引：
  - `idx_follow_tag_mid`：按 mid 列出我的分组；
  - `idx_follow_tag_user_tag`：按分组取成员（列表页按分组过滤走这条）；
  - `idx_follow_tag_user_target`：按「我关注的人」反查其所属分组（列表页回填分组标签用）。
"""

from sqlalchemy import BIGINT, Index, UniqueConstraint
from sqlmodel import Field, SQLModel

from app.models.db.base_tbl import TimestampMixin


class UserFollowTag(TimestampMixin, table=True):
    """用户自建关注分组（系统分组不落库）。"""

    __tablename__ = "msg_user_follow_tag"
    __table_args__ = (
        UniqueConstraint("mid", "name", name="uq_user_follow_tag_mid_name"),
        Index("idx_follow_tag_mid", "mid", "created_at"),
        {"extend_existing": True},
    )

    id: int | None = Field(default=None, primary_key=True)

    mid: int = Field(sa_type=BIGINT, index=True, description="分组所属用户 mid")
    name: str = Field(max_length=20, description="分组名（B 站上限 20 字）")
    tip: str | None = Field(
        default=None,
        max_length=100,
        description="分组简介（选填，对标 B 站 tip）",
    )


class UserFollowTagUser(TimestampMixin, table=True):
    """分组-成员关系（一个关注对象可属于多个分组）。

    **必须带 owner `mid`**：系统分组「特别关注」的 `tag_id = -10` 是全用户共用的约定值，
    若唯一键只取 `(tag_id, target_mid)`，A 把 123 设为特别关注就会与 B 的同样操作串号；
    因此唯一键是 `(mid, tag_id, target_mid)`，分组归属始终由 owner 决定。
    """

    __tablename__ = "msg_user_follow_tag_user"
    __table_args__ = (
        UniqueConstraint(
            "mid", "tag_id", "target_mid", name="uq_user_follow_tag_user_tag"
        ),
        Index("idx_follow_tag_user_tag", "tag_id"),
        # 列表页按「我 + 关注对象」回填其所属分组
        Index("idx_follow_tag_user_mid_target", "mid", "target_mid"),
        {"extend_existing": True},
    )

    id: int | None = Field(default=None, primary_key=True)

    mid: int = Field(
        sa_type=BIGINT, index=True, description="关系归属用户（发起分组的人）"
    )
    tag_id: int = Field(
        sa_type=BIGINT, description="分组 id（自建分组为正数，-10 为特别关注）"
    )
    target_mid: int = Field(sa_type=BIGINT, description="被分组关注的用户 mid")


__all__ = ["SQLModel", "UserFollowTag", "UserFollowTagUser"]
