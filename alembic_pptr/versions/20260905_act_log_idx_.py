"""add idx_log_mid_created on TUserActInfoLog（空间「上次登录」查询加速）

背景（2.58.0）：个人空间主页要展示「上次登录时间 + IP 属地」，数据源是 pptr 的
`TUserActInfoLog`（每用户登录一条，`act_info` = `daily_login` / `login_succ`）。
该表在 `PptrUserActInfoLog.__table_args__`（`app/models/db/../pptr_db.py:164-190`）
里**只有主键与外键，没有任何 mid / createdAt 索引**，按「某用户最近一条登录记录」
查询（`WHERE mid=? AND act_info IN (...) ORDER BY createdAt DESC LIMIT 1`）会全表扫。

同一张表既有查询 `PptrUser.list_act_log`（用户中心「我的记录」，
`services/user/account/base.py:1135-1145`）也是同样的 `mid + createdAt` 过滤，
本次加索引两者一起受益。

⚠️ 本库是上游 pptr Postgres，be-message 只是连上去读写；迁移仅在开发 / 生产库
`CREATE INDEX` 一张表的一个索引，不改动任何既有列。

Revision ID: 20260905_act_log_idx
Revises: d366ed68066f
Create Date: 2026-09-05 11:00:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "20260905_act_log_idx"
down_revision: Union[str, Sequence[str], None] = "d366ed68066f"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema：为 `TUserActInfoLog` 加 `(mid, createdAt DESC)` 复合索引。"""
    op.create_index(
        "idx_log_mid_created",
        "TUserActInfoLog",
        ["mid", sa.literal_column('"createdAt" DESC')],
        unique=False,
    )


def downgrade() -> None:
    """Downgrade schema：删索引（查询退回全表扫，功能不受影响）。"""
    op.drop_index("idx_log_mid_created", table_name="TUserActInfoLog")
