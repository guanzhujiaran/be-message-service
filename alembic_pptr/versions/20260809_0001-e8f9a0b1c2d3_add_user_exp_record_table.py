"""add TUserExpRecord table

- add TUserExpRecord table for experience tracking (action_type stored as int)

Revision ID: e8f9a0b1c2d3
Revises: b739f99505b8
Create Date: 2026-08-09 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'e8f9a0b1c2d3'
down_revision: Union[str, None] = 'b739f99505b8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'TUserExpRecord',
        sa.Column('pk', sa.BIGINT(), autoincrement=True, nullable=False),
        sa.Column('mid', sa.BIGINT(), nullable=False),
        sa.Column('createdAt', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('CURRENT_TIMESTAMP')),
        sa.Column('action_type', sa.Integer(), nullable=False, comment='行为类型 int：1=daily_login（每日登录），与 ExpActionType 枚举对应'),
        sa.Column('exp', sa.Integer(), nullable=False, server_default=sa.text('0'), comment='本次增加的经验值'),
        sa.Column('ref_date', sa.String(10), nullable=False, comment='行为引用日期，格式 YYYY-MM-DD，用于每日/每周行为幂等检查'),
        sa.ForeignKeyConstraint(['mid'], ['TUserInfo.uid'], name='TUserExpRecord_mid_fkey'),
        sa.PrimaryKeyConstraint('pk', name='TUserExpRecord_pkey'),
        comment='用户经验增加记录表，记录所有行为（每日登录、发评论等）增加的经验值',
    )
    op.create_index('idx_exp_record_mid_action_ref_date', 'TUserExpRecord', ['mid', 'action_type', 'ref_date'])


def downgrade() -> None:
    op.drop_index('idx_exp_record_mid_action_ref_date', table_name='TUserExpRecord')
    op.drop_table('TUserExpRecord')