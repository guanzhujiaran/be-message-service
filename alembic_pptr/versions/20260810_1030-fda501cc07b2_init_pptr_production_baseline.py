"""init_pptr_production_baseline

Revision ID: fda501cc07b2
Revises: 
Create Date: 2026-08-10 10:30:53.817278

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel


# revision identifiers, used by Alembic.
revision: str = 'fda501cc07b2'
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 基线：直接以当前生产库（PPTR_Bili_Lot）的真实结构为准，
    # 不做任何结构变更。后续演进通过 autogenerate 生成的新版本完成。
    pass


def downgrade() -> None:
    pass
