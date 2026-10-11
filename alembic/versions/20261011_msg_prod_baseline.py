"""init_message_production_baseline

主库（`BiliMessageDB`）基线：**以生产库现状为准，本条不含任何结构变更**。

2026-10-11：主库已与生产环境对齐，故以生产库真实结构为基线重做版本管理，
把此前零散的增量迁移全部作废，压缩为「基线 + 一次结构性同步」两条，本条即基线。
与 pptr 库（`alembic_pptr/versions/` 的 `init_pptr_production_baseline`）同款口径，
见 `docs/be-message-统一计划书.md` §5.28。

用法：

- **存量生产库**：`alembic stamp 20261011_msg_prod_baseline` 即视为已对齐，
  再 `alembic upgrade head` 执行下一条 `sync_models_to_production`；
- 本条**不支持从零建库**——基线就是「库已经是生产结构」这个既成事实。
  私信正文分片等本就不由 Alembic 管理（`app/core/sharding.py` 运行时懒创建），
  新环境一律从生产库快照恢复，不存在空库建表路径。

> 注意：revision id 必须 <= 32 字符——`alembic_version.version_num` 是 `VARCHAR(32)`，
> 超长会在 `stamp` / `upgrade` 时直接报 `Data too long for column 'version_num'`。

Revision ID: 20261011_msg_prod_baseline
Revises:
Create Date: 2026-10-11 14:51:03.000000

"""

from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "20261011_msg_prod_baseline"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema：基线不含结构变更，直接以生产库现状为准。"""
    pass


def downgrade() -> None:
    pass