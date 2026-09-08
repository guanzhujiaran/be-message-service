"""add STRANGER to dmsessiontypeenum

Revision ID: 9c1a2b3d4e5f
Revises: fcf7bd3563de
Create Date: 2026-09-07 10:00:00.000000

把 ``msg_dm_session.session_type`` 的 ENUM 扩展为 ``('SINGLE', 'STRANGER')``。

历史数据不需要回填：原枚举只有 ``SINGLE``，存量行均为 SINGLE；
新增的 STRANGER 分类只由拦截命中（filtered=True）的新消息产生。
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision: str = "9c1a2b3d4e5f"
down_revision: Union[str, Sequence[str], None] = "fcf7bd3563de"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """扩展 session_type ENUM 新增 STRANGER。"""
    # MySQL 原生 ENUM 通过 MODIFY COLUMN 追加新值，列上原有数据保持不变。
    op.execute(
        "ALTER TABLE msg_dm_session "
        "MODIFY COLUMN session_type "
        "ENUM('SINGLE', 'STRANGER') NOT NULL"
    )


def downgrade() -> None:
    """回滚：若已有 STRANGER 行则降级失败，避免数据丢失。"""
    bind = op.get_bind()
    has_stranger = bind.execute(
        sa.text("SELECT COUNT(*) FROM msg_dm_session WHERE session_type = 'STRANGER'")
    ).scalar()
    if has_stranger:
        raise RuntimeError(
            "msg_dm_session 存在 session_type=STRANGER 的行，无法安全回滚到仅 SINGLE 的 ENUM；"
            "请先手动迁移这些行后再执行 downgrade"
        )
    op.execute(
        "ALTER TABLE msg_dm_session "
        "MODIFY COLUMN session_type ENUM('SINGLE') NOT NULL"
    )


__all__ = ["downgrade", "upgrade"]
