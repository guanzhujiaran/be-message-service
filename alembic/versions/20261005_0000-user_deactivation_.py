"""create msg_user_deactivation (用户注销冷静期)

两阶段注销：提交注销进入 N 天冷静期（cooling），数据保留、可由重新登录自动撤销；
到期由定时任务物理删除本地数据后置 pending_casdoor，再调 Casdoor delete-user，
成功删记录、失败落此表由补偿任务重试。

仅改 MySQL 主库；pptr Postgres 结构不变。

Revision ID: 20261005_user_deactivation
Revises: 20260905_space_privacy
Create Date: 2026-10-05 00:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision: str = "20261005_user_deactivation"
down_revision: Union[str, Sequence[str], None] = "20260905_space_privacy"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema：建用户注销冷静期状态表。"""
    op.create_table(
        "msg_user_deactivation",
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("mid", sa.BIGINT(), nullable=False, comment="用户 mid"),
        sa.Column("user_name", sa.String(length=128), nullable=True, comment="Casdoor name"),
        sa.Column(
            "status",
            sa.String(length=16),
            nullable=False,
            server_default="cooling",
            comment="cooling / pending_casdoor",
        ),
        sa.Column("deactivated_at", sa.DateTime(), nullable=False, comment="提交注销时刻"),
        sa.Column(
            "delete_after",
            sa.DateTime(),
            nullable=False,
            comment="到期时刻（=deactivated_at + 冷静期天数）",
        ),
        sa.Column(
            "casdoor_delete_error",
            sa.String(length=512),
            nullable=True,
            comment="最近一次 Casdoor 删除失败原因",
        ),
        sa.Column(
            "retry_count",
            sa.Integer(),
            nullable=False,
            server_default="0",
            comment="Casdoor 删除重试次数",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("mid", name="uq_user_deactivation_mid"),
        comment="用户注销冷静期状态（两阶段注销）",
    )
    op.create_index(
        op.f("ix_msg_user_deactivation_created_at"),
        "msg_user_deactivation",
        ["created_at"],
        unique=False,
    )
    op.create_index(
        op.f("ix_msg_user_deactivation_mid"),
        "msg_user_deactivation",
        ["mid"],
        unique=False,
    )
    op.create_index(
        op.f("ix_msg_user_deactivation_status"),
        "msg_user_deactivation",
        ["status"],
        unique=False,
    )
    op.create_index(
        op.f("ix_msg_user_deactivation_delete_after"),
        "msg_user_deactivation",
        ["delete_after"],
        unique=False,
    )


def downgrade() -> None:
    """Downgrade schema：删注销状态表。"""
    op.drop_index(
        op.f("ix_msg_user_deactivation_delete_after"),
        table_name="msg_user_deactivation",
    )
    op.drop_index(
        op.f("ix_msg_user_deactivation_status"),
        table_name="msg_user_deactivation",
    )
    op.drop_index(
        op.f("ix_msg_user_deactivation_mid"),
        table_name="msg_user_deactivation",
    )
    op.drop_index(
        op.f("ix_msg_user_deactivation_created_at"),
        table_name="msg_user_deactivation",
    )
    op.drop_table("msg_user_deactivation")
