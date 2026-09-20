"""sync InteractionBizTypeEnum native ENUM columns (OTHERS_LOT_DYN / RPA_TAG)

背景：`InteractionBizTypeEnum` 在模型里以 `SAEnum(...)` 落库 → MySQL **原生 ENUM 存成员名**，
枚举新增成员必须同步 ALTER 全部相关列，否则写入新类型会报
`(1265, "Data truncated for column 'xxx' at row 1")`。

本迁移补齐两个成员：
- `OTHERS_LOT_DYN = 15`（2.61.0 新增，第三方抽奖动态，bizId = `biliopusdb.t_lotdyninfo.dynId`，
  与 `LOTTERY` 是两个独立命名空间）；
- `RPA_TAG = 14`（**存量遗漏**：模型早已在用该成员，但初始建表迁移的 ENUM 定义里没有它，
  一旦 RPA 标签写入 msg_event / 互动表同样会 1265，故一并补齐）。

注意列形态差异：实际库里多数 `bizType` / `type` / `source_type` 是原生 ENUM，
但 `TResourceReport.bizType` 是 `int`（存数值，天然支持新成员）——非 `enum(` 开头的列直接跳过。

本迁移**幂等且只追加**：读取 `information_schema` 里的现有 `COLUMN_TYPE`，
在末尾追加缺失成员（不重排既有成员，避免改变 ENUM 索引序 / 排序语义），已存在则跳过。

**必须在线执行**：迁移依赖运行时查询 `information_schema` 来判断列当前形态与已有成员，
故不支持 `alembic upgrade head --sql` 离线生成 SQL（离线模式无真实连接）。
如需先审阅，可先在生产库跑下方的只读核对语句（见 `_ENUM_COLUMNS` 列清单），确认列形态后再在线执行。

Revision ID: 20260920_others_lot_dyn_enum
Revises: 1cb38c34aef9
Create Date: 2026-09-20 13:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision: str = "20260920_others_lot_dyn_enum"
down_revision: Union[str, Sequence[str], None] = "1cb38c34aef9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


#: 需要补齐的成员（**成员名**，原生 ENUM 存名字）：模型在用但 ENUM 定义里缺失的全部成员
_NEW_MEMBERS: list[str] = ["OTHERS_LOT_DYN", "RPA_TAG"]

#: 全部以 SAEnum(InteractionBizTypeEnum) 落库的 (表名, 列名)。
#: 表名/列名大小写与模型 `__tablename__` / 字段名保持一致（`information_schema` 用实际库名匹配）。
_ENUM_COLUMNS: list[tuple[str, str]] = [
    ("msg_comment_subject", "type"),
    ("msg_comment_index", "type"),
    ("msg_comment_at", "type"),
    ("msg_event", "source_type"),
    ("TFeedImpression", "bizType"),
    ("TResourceFeed", "bizType"),
    ("TMoment", "bizType"),
    ("TResourceLike", "bizType"),
    ("TResourceDislike", "bizType"),
    ("TResourceFavorite", "bizType"),
    ("TResourceReport", "bizType"),
    ("TResourceAuditLog", "bizType"),
]


def _read_enum_column(bind, table: str, column: str):
    """读取列定义；表 / 列不存在返回 None（不同环境建表进度可能不同，跳过而非报错）。"""
    row = bind.execute(
        sa.text(
            "SELECT COLUMN_TYPE, IS_NULLABLE, COLUMN_DEFAULT "
            "FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA = DATABASE() "
            "AND TABLE_NAME = :table AND COLUMN_NAME = :column"
        ),
        {"table": table, "column": column},
    ).first()
    return row


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
    # 只有原生 ENUM 列需要同步成员名；实际库里部分 bizType 列是 INT（存数值，如 15），
    # 天然支持新成员，直接跳过——否则会把 "int" 误当 ENUM 定义拼出非法 SQL。
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
    """Upgrade schema：为全部 `InteractionBizTypeEnum` 原生 ENUM 列补齐缺失成员。"""
    bind = op.get_bind()
    for table, column in _ENUM_COLUMNS:
        for member in _NEW_MEMBERS:
            _add_member(bind, table, column, member)


def downgrade() -> None:
    """Downgrade schema：移除本次补齐的成员（需先清理掉使用这些成员的存量行）。"""
    bind = op.get_bind()
    for table, column in _ENUM_COLUMNS:
        for member in _NEW_MEMBERS:
            _remove_member(bind, table, column, member)
