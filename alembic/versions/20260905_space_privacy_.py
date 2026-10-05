"""create TUserSpacePrivacy (空间对外可见性设置)

2.58.0 新增：个人空间「别人能看到什么」的开关表，粒度参考 B 站空间「隐私设置」
（按内容模块开关注册器对外展示沿用既有 `TUserFavoriteSetting.showFavorites`，不重复建列）。

设计口径：
- 每用户一行（`mid` 主键），懒创建，**默认全部不展示**（与 `showFavorites` 默认展示相反），
  避免新建账号即对外泄露邮箱 / IP 属地；
- 统计数字（关注数 / 粉丝数 / 获赞 / 动态 / 被访问次数）固定展示，**不设开关**，故不在本表；
- 不设「总开关」：5 个独立开关再套一层只会让状态组合爆炸。

`mid` 用 BIGINT int（对齐 `msg_user_setting` / `TUserFavoriteSetting` 既有约定），
前端传 str 由接口层 `StrInt` 包装转换，不在 DB 层做字符串存储。

Revision ID: 20260905_space_privacy
Revises: 20260920_stat_viewlog_enum
Create Date: 2026-09-05 10:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "20260905_space_privacy"
down_revision: Union[str, Sequence[str], None] = "20260920_stat_viewlog_enum"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema：建空间可见性表（bool 列落 MySQL TINYINT(1)）。"""
    op.create_table(
        "TUserSpacePrivacy",
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("mid", sa.BIGINT(), autoincrement=False, nullable=False),
        sa.Column(
            "show_personal_info",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "show_login_info", sa.Boolean(), nullable=False, server_default=sa.text("0")
        ),
        sa.Column(
            "show_follow_list",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "show_like_list", sa.Boolean(), nullable=False, server_default=sa.text("0")
        ),
        sa.Column(
            "show_fans_list", sa.Boolean(), nullable=False, server_default=sa.text("0")
        ),
        sa.PrimaryKeyConstraint("mid", name="TUserSpacePrivacy_pkey"),
        comment="空间对外可见性：每用户一行，默认全关",
    )
    op.create_index(
        op.f("ix_TUserSpacePrivacy_created_at"),
        "TUserSpacePrivacy",
        ["created_at"],
        unique=False,
    )


def downgrade() -> None:
    """Downgrade schema：删表（回滚后隐私设置回落「全部不展示」的默认口径）。"""
    op.drop_index(
        op.f("ix_TUserSpacePrivacy_created_at"), table_name="TUserSpacePrivacy"
    )
    op.drop_table("TUserSpacePrivacy")
