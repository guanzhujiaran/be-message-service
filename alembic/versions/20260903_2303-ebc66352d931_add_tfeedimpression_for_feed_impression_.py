"""add TFeedImpression for feed impression dedup

Revision ID: ebc66352d931
Revises: f2712dc213c3
Create Date: 2026-09-03 23:03:40.141358

注意：自动生成时附带了约 20 处既有索引的 drop/create（模型声明的 DESC 索引与
MySQL 实际存储形态的固有 diff，非本次改动引入），已手工裁剪，本迁移**只**新增
曝光表及其索引，避免对既有表做无意义的索引重建。
"""
from typing import Sequence, Union

import sqlmodel
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'ebc66352d931'
down_revision: Union[str, Sequence[str], None] = 'f2712dc213c3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'TFeedImpression',
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.Column('pk', sa.BIGINT(), autoincrement=True, nullable=False),
        sa.Column('viewerKey', sqlmodel.sql.sqltypes.AutoString(length=96), nullable=False),
        sa.Column('bizType', sa.Enum('DYNAMIC', 'LOTTERY', 'RPA_ACTION', 'RPA_WORKFLOW', 'RPA_BROWSER', 'RPA_PLUGIN', 'COMMENT', 'USER', name='interactionbiztypeenum'), nullable=False),
        sa.Column('bizId', sa.BIGINT(), nullable=False),
        sa.Column('feedScene', sqlmodel.sql.sqltypes.AutoString(length=32), nullable=False),
        sa.Column('impressionCount', sa.Integer(), nullable=False),
        sa.Column('firstImpressionAt', sa.DateTime(), nullable=False),
        sa.Column('lastImpressionAt', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('pk', name='TFeedImpression_pkey'),
        sa.UniqueConstraint('viewerKey', 'bizType', 'bizId', 'feedScene', name='TFeedImpression_viewerKey_bizType_bizId_feedScene_key'),
        comment='通用 Feed 曝光记录：观众在某场景已下发资源（曝光去重，防重复刷到）',
    )
    op.create_index('idx_feed_impression_viewer_scene_time', 'TFeedImpression', ['viewerKey', 'feedScene', sa.literal_column('lastImpressionAt DESC')], unique=False)
    op.create_index(op.f('ix_TFeedImpression_created_at'), 'TFeedImpression', ['created_at'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f('ix_TFeedImpression_created_at'), table_name='TFeedImpression')
    op.drop_index('idx_feed_impression_viewer_scene_time', table_name='TFeedImpression')
    op.drop_table('TFeedImpression')
