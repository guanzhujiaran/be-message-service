"""Alembic env：接管 pptr 的 Postgres 库 `PTR_Bili_Lot`（baseline 后由本服务管理版本）。

与 be-message 主 Alembic（alembic/，管理 MySQL 元数据库）相互独立：
- 本 env 连 Postgres（settings.postgres_pptr_url 的同步驱动版）；
- target_metadata 仍是全局 `SQLModel.metadata`，但通过 `include_object` 仅纳管
  schema = 'public' 的 pptr 表（即 app.models.pptr_db 中定义的表），避免把 MySQL 表
  误同步进 Postgres。
"""
# 让 alembic 能 import app.*
import sys
from pathlib import Path

from sqlalchemy import engine_from_config, pool
from sqlmodel import SQLModel

from alembic import context

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import settings  # noqa: E402

# 导入 pptr 模型，确保表注册进 SQLModel.metadata
from app.models import pptr_db

config = context.config

# Postgres 同步驱动（asyncpg -> psycopg2）
SYNC_URL = settings.postgres_pptr_url.replace(
    "postgresql+asyncpg://", "postgresql+psycopg2://"
)
config.set_main_option("sqlalchemy.url", SYNC_URL)

target_metadata = SQLModel.metadata

# 仅纳管 pptr 库表（app.models.pptr_db 中定义的所有表名），排除 MySQL 元数据库表。
# 注意：pptr 表未指定 schema（依赖 Postgres 默认 public search_path），
# 故不能靠 schema 过滤，改用显式表名集合（用 __tablename__）。
PPTR_TABLE_NAMES = {
    getattr(obj, "__tablename__", None)
    for name in pptr_db.__all__
    if (obj := getattr(pptr_db, name, None)) and getattr(obj, "__tablename__", None)
}


def include_object(object, name, type_, reflected, compare_to):
    """仅纳管 pptr_db 中定义的表，排除 be-message 的 MySQL 元数据库表。"""
    if type_ == "table":
        return name in PPTR_TABLE_NAMES
    # 索引/约束等附属对象随其所属表一起纳入
    return True


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        include_object=include_object,
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            include_object=include_object,
            compare_type=True,
            compare_server_default=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
