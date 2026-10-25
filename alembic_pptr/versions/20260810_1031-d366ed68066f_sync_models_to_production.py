"""pptr 库基线（压缩后）：库已是 sync 之后的状态

压缩前本条是 `sync_models_to_production`（建 `TUserExpRecord` + 索引 + 唯一约束、
drop 22 张孤儿表），它的上游还有一条空 DDL 基线 `fda501cc07b2`。

§5.29 把 pptr 版本链从 3 条压成 2 条：本条**提升为新根**（`down_revision=None`，
空 `upgrade()` / `downgrade()`），语义变为「库已经是 sync 之后的状态」；
唯一的真实演进 `20260905_act_log_idx` 挂在本条之后作为 head。

保留 `d366ed68066f` 这个 id 而非另起新 id，是为了让停在它上面的生产库
能直接 `upgrade head` 补索引、无需 stamp（详见计划书 §5.29）。

⚠️ 代价：sync 的 DDL 已从版本链上移除，不再支持「未 sync 的原始生产快照」
直接 upgrade（会缺 `TUserExpRecord` 与 `TUserDetail_uname_key`）。
遇到这种库请人工补跑 sync 内容，或单独出一条迁移承接。

Revision ID: d366ed68066f
Revises: （无，压缩后的根）
Create Date: 2026-08-10 10:31:56.093579
"""

from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "d366ed68066f"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """基线：以库现状为准，不做任何结构变更。"""
    pass


def downgrade() -> None:
    """基线：无可回退内容。"""
    pass
