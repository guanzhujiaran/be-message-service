"""add_tinteraction_viewlog

Revision ID: b7c4d8e2f9a3
Revises: f3a5b9c7d1e2
Create Date: 2026-08-20 10:00:00.000000

2.23.0：浏览量统计泛化到非动态资源（lottery / rpa_*）：
- TInteractionStat 新增 viewCount 列（浏览数，TInteractionViewLog 去重 + 首次原子 +1）
- 新增通用浏览去重表 TInteractionViewLog（对标 TMomentViewLog，
  UniqueConstraint(bizType, bizId, mid, refDate) → 同用户同资源同天只计一次 Stat 浏览量）
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b7c4d8e2f9a3'
down_revision: Union[str, Sequence[str], None] = 'f3a5b9c7d1e2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # 1. TInteractionStat 新增 viewCount 列（默认 0）
    op.add_column(
        'TInteractionStat',
        sa.Column('viewCount', sa.BIGINT(), server_default='0', nullable=False),
    )

    # 2. 新增通用浏览去重表 TInteractionViewLog
    op.create_table(
        'TInteractionViewLog',
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.Column('pk', sa.BIGINT(), autoincrement=True, nullable=False),
        sa.Column('bizType', sa.Integer(), nullable=False),
        sa.Column('bizId', sa.BIGINT(), nullable=False),
        sa.Column('mid', sa.BIGINT(), nullable=False),
        sa.Column('refDate', sa.String(length=10), nullable=False),
        sa.Column('viewCount', sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint('pk', name='TInteractionViewLog_pkey'),
        sa.UniqueConstraint(
            'bizType', 'bizId', 'mid', 'refDate', name='TInteractionViewLog_bizType_bizId_mid_refDate_key'
        ),
        comment='通用浏览去重表：同用户同天同非动态资源只计一次浏览量'
    )
    op.create_index(op.f('ix_TInteractionViewLog_created_at'), 'TInteractionViewLog', ['created_at'], unique=False)
    op.create_index('idx_viewlog_biz', 'TInteractionViewLog', ['bizType', 'bizId'], unique=False)
    op.create_index('idx_viewlog_mid_date', 'TInteractionViewLog', ['mid', 'refDate'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('idx_viewlog_mid_date', table_name='TInteractionViewLog')
    op.drop_index('idx_viewlog_biz', table_name='TInteractionViewLog')
    op.drop_index(op.f('ix_TInteractionViewLog_created_at'), table_name='TInteractionViewLog')
    op.drop_table('TInteractionViewLog')
    op.drop_column('TInteractionStat', 'viewCount')
