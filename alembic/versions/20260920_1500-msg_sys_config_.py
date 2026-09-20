"""create msg_sys_config (运行时系统配置)

2.64.0 新增：可热更新的运营参数落库位置（key 主键 + JSON value）。
读取走 `app/services/common/runtime_config.py`（进程内 TTL 缓存 + DB 权威），
写入走管理端 `/api/v1/message/admin/sys-config/update`。

为什么不引入 Redis：be-message 约定「所有数据直接落 MySQL」（见 app/core/database.py），
且限流阈值调整对「改完到全实例一致」的延迟不敏感（≤ TTL 10s 足够）。

表为空也能正常启动：读取方在缺 key 时回落 `settings` 默认值。

Revision ID: 20260920_msg_sys_config
Revises: 20260920_others_lot_dyn_enum
Create Date: 2026-09-20 15:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision: str = "20260920_msg_sys_config"
down_revision: Union[str, Sequence[str], None] = "20260920_others_lot_dyn_enum"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema：建运行时配置表（`key` 为 MySQL 保留字，由方言自动反引号处理）。"""
    op.create_table(
        "msg_sys_config",
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("value", sa.JSON(), nullable=False),
        sa.Column("remark", sa.String(length=255), nullable=True),
        sa.Column("updatedBy", sa.BIGINT(), nullable=False),
        sa.PrimaryKeyConstraint("key"),
        comment="运行时系统配置（2.64.0）：key → JSON value，管理端热更新、读取侧 TTL 缓存",
    )
    op.create_index(
        op.f("ix_msg_sys_config_created_at"),
        "msg_sys_config",
        ["created_at"],
        unique=False,
    )


def downgrade() -> None:
    """Downgrade schema：删表（配置为运行时覆盖项，删除后回落 settings 默认值）。"""
    op.drop_index(op.f("ix_msg_sys_config_created_at"), table_name="msg_sys_config")
    op.drop_table("msg_sys_config")
