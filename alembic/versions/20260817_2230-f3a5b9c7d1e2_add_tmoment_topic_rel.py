"""add_tmoment_topic_rel

Revision ID: f3a5b9c7d1e2
Revises: e11993cc8480
Create Date: 2026-08-17 22:30:00.000000

2.22.0：新增动态-话题多对多关系表 TMomentTopicRel
（一条动态多话题，上限 5；TMoment.topicId 保留为主话题）。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f3a5b9c7d1e2'
down_revision: Union[str, Sequence[str], None] = 'e11993cc8480'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('TMomentTopicRel',
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.Column('pk', sa.BIGINT(), autoincrement=True, nullable=False),
    sa.Column('dynId', sa.BIGINT(), nullable=False),
    sa.Column('topicId', sa.BIGINT(), nullable=False),
    sa.ForeignKeyConstraint(['dynId'], ['TMoment.dynId'], name='TMomentTopicRel_dynId_fkey', ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('pk', name='TMomentTopicRel_pkey'),
    sa.UniqueConstraint('dynId', 'topicId', name='TMomentTopicRel_dynId_topicId_key'),
    comment='动态-话题多对多关系：一条动态多话题（上限5），TMoment.topicId=主话题'
    )
    op.create_index(op.f('ix_TMomentTopicRel_created_at'), 'TMomentTopicRel', ['created_at'], unique=False)
    op.create_index('idx_topic_rel_topic', 'TMomentTopicRel', ['topicId', 'dynId'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('idx_topic_rel_topic', table_name='TMomentTopicRel')
    op.drop_index(op.f('ix_TMomentTopicRel_created_at'), table_name='TMomentTopicRel')
    op.drop_table('TMomentTopicRel')
