"""sync InteractionBizTypeEnum native ENUM for TInteractionStat / TInteractionViewLog

背景（2.63.1 补漏）：上一版 `20260920_others_lot_dyn_enum` 只列了「互动明细 / 事件」类表的
`bizType`，**漏掉了 `InteractionStatBase` / `InteractionViewLogBase` 两基类落的库表**
（`TInteractionStat.bizType` / `TInteractionViewLog.bizType`）——它们同样以
`SAEnum(InteractionBizTypeEnum)` 落库为 **MySQL 原生 ENUM 存成员名**，缺成员写入即报
`(1265, "Data truncated for column 'bizType' at row 1")`。

现象：`others_lot_dyn`（2.61.0 新增，bizType=15）的浏览上报在
`TInteractionViewLog` 插入失败 → 消费者 nack 重投 → 无限打转（见 §「重试计数」修复）。

本迁移同步这两个列到 `InteractionBizTypeEnum` 的**全量成员**（幂等、只追加、不重排），
避免以后再新增成员时重复踩坑。

**必须在线执行**：迁移依赖运行时查询 `information_schema` 判断列当前形态，
不支持 `alembic upgrade head --sql` 离线生成 SQL。

Revision ID: 20260920_stat_viewlog_enum
Revises: 20260920_msg_sys_config
Create Date: 2026-09-20 22:30:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

from bili_common.models.interaction import InteractionBizTypeEnum


# revision identifiers, used by Alembic.
revision: str = "20260920_stat_viewlog_enum"
down_revision: Union[str, Sequence[str], None] = "20260920_msg_sys_config"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# 上一版迁移未覆盖、本次补齐的 (表名, 列名)：
# 计数表 TInteractionStat 与浏览去重明细 TInteractionViewLog（基类字段，易被遗漏）。
_ENUM_COLUMNS: list[tuple[str, str]] = [
    ("TInteractionStat", "bizType"),
    ("TInteractionViewLog", "bizType"),
]

# 同步到枚举全量成员（只追加，不重排既有成员，避免改变 ENUM 索引序 / 排序语义）
_SYNC_MEMBERS: list[str] = [member.name for member in InteractionBizTypeEnum]

# downgrade 只回滚本次真正新增的成员（存量行可能已在使用，回滚前需先清理）
_ROLLBACK_MEMBERS: list[str] = ["OTHERS_LOT_DYN", "RPA_TAG"]


def _read_enum_column(bind, table: str, column: str):
    """读取列定义；表 / 列不存在返回 None（不同环境建表进度不同，跳过而非报错）。"""
    return bind.execute(
        sa.text(
            "SELECT COLUMN_TYPE, IS_NULLABLE, COLUMN_DEFAULT "
            "FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA = DATABASE() "
            "AND TABLE_NAME = :table AND COLUMN_NAME = :column"
        ),
        {"table": table, "column": column},
    ).first()


def _alter_enum_column(bind, table: str, column: str, new_type: str, nullable: str, default) -> None:
    """按现有列的 NULL / DEFAULT 约束改写列类型（原生 SQL，避免 alembic 想当然地变更约束）。"""
    null_sql = "" if nullable == "YES" else " NOT NULL"
    if default is None:
        default_sql = ""
    elif str(default).upper() == "NULL":
        default_sql = " DEFAULT NULL"
    else:
        default_sql = f" DEFAULT '{default}'"
    bind.execute(
        sa.text(
            f"ALTER TABLE `{table}` MODIFY COLUMN `{column}` {new_type}{null_sql}{default_sql}"
        )
    )


def _add_member(bind, table: str, column: str, member: str) -> None:
    row = _read_enum_column(bind, table, column)
    if row is None:
        return
    column_type, nullable, default = row
    # 只有原生 ENUM 列需要同步成员名；INT 列（存数值）天然支持新成员，跳过
    if not column_type.lower().startswith("enum("):
        return
    if f"'{member}'" in column_type:
        return
    # COLUMN_TYPE 形如 enum('DYNAMIC','LOTTERY',...) → 末尾追加成员
    new_type = f"{column_type[:-1]},'{member}')"
    _alter_enum_column(bind, table, column, new_type, nullable, default)


def _remove_member(bind, table: str, column: str, member: str) -> None:
    row = _read_enum_column(bind, table, column)
    if row is None:
        return
    column_type, nullable, default = row
    if not column_type.lower().startswith("enum("):
        return
    if f"'{member}'" not in column_type:
        return
    parts = [p for p in column_type[len("enum(") : -1].split(",") if p.strip("'") != member]
    if not parts:
        return
    new_type = f"enum({','.join(parts)})"
    _alter_enum_column(bind, table, column, new_type, nullable, default)


def upgrade() -> None:
    """Upgrade schema：把两列的原生 ENUM 同步到 `InteractionBizTypeEnum` 全量成员。"""
    bind = op.get_bind()
    for table, column in _ENUM_COLUMNS:
        for member in _SYNC_MEMBERS:
            _add_member(bind, table, column, member)


def downgrade() -> None:
    """Downgrade schema：移除本次补齐的新成员（需先清理掉使用这些成员的存量行）。"""
    bind = op.get_bind()
    for table, column in _ENUM_COLUMNS:
        for member in _ROLLBACK_MEMBERS:
            _remove_member(bind, table, column, member)
