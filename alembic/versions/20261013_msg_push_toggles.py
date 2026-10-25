"""msg_user_setting 增加互动事件外推开关

给站内互动提醒（回复/评论、@、点赞）各加一个**外部渠道推送**开关，
颗粒度与站内 `recv_*` 一致，默认关闭；仅在站内对应开关开启时才可能生效
（站内关了，站外也不推）。

Revision ID: 20261013_msg_push_toggles
Revises: sync_to_prod
Create Date: 2026-10-13 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "20261013_msg_push_toggles"
down_revision: str | Sequence[str] | None = "sync_to_prod"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema：加列，存量行回填为「关闭」。"""
    op.add_column(
        "msg_user_setting",
        sa.Column(
            "push_reply",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("0"),
            comment="回复/评论是否推送到外部渠道",
        ),
    )
    op.add_column(
        "msg_user_setting",
        sa.Column(
            "push_at",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("0"),
            comment="@提醒是否推送到外部渠道",
        ),
    )
    op.add_column(
        "msg_user_setting",
        sa.Column(
            "push_like",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("0"),
            comment="点赞是否推送到外部渠道",
        ),
    )


def downgrade() -> None:
    """Downgrade schema：与 upgrade 严格对称，移除三列。"""
    op.drop_column("msg_user_setting", "push_like")
    op.drop_column("msg_user_setting", "push_at")
    op.drop_column("msg_user_setting", "push_reply")
