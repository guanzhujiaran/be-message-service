"""add uname unique constraint

Revision ID: b739f99505b8
Revises: 5ccc64b8ab8a
Create Date: 2026-08-08 21:07:47.751018

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'b739f99505b8'
down_revision: Union[str, None] = '5ccc64b8ab8a'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 仅新增 uname 唯一约束（对齐 B 站昵称不可重复规则）。
    # 注：autogenerate 还探测到库与模型之间多处历史差异（如 SequelizeMeta 表、
    # 各表 column comment / 主键 identity / 外键等），这些属于生产库由 Sequelize
    # 创建、与当前 SQLAlchemy 模型的既有偏差，与本次诉求无关，故未纳入本迁移，
    # 相关结构已由基线版本(5ccc64b8ab8a)以 stamp 方式确认。
    op.create_unique_constraint('TUserDetail_uname_key', 'TUserDetail', ['uname'])


def downgrade() -> None:
    op.drop_constraint('TUserDetail_uname_key', 'TUserDetail', type_='unique')
