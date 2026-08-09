# -*- coding: utf-8 -*-
"""be-message-service Alembic 异步环境配置。

只管理**主库**（元数据库）的 Schema：系统通知、事件提醒、私信索引 / 会话、
消息设置、用户活跃度等表。

私信内容的「月度分库 + 库内 100 张分表」不纳入 Alembic：
月份是无限增长的维度，写进版本脚本会导致迁移文件无限膨胀且无法回滚，
改由 app/core/sharding.py 在运行时用 `CREATE ... IF NOT EXISTS` 懒创建。
"""

import asyncio
import sys
from pathlib import Path

from alembic import context
from sqlalchemy.ext.asyncio import create_async_engine
from sqlmodel import SQLModel

# 确保项目根目录在 sys.path 中，以便导入 app 模块
_current_dir = Path(__file__).resolve().parent
sys.path.insert(0, str(_current_dir.parent))

from app.core.config import settings  # noqa: E402

# 导入所有表模型，确保注册到 SQLModel.metadata（autogenerate 依赖）
from app.models.db import *  # noqa: F401, F403, E402

target_metadata = SQLModel.metadata

# 临时指向空 MySQL 数据库生成全量 base migration，生成后改回 settings.mysql_message_url
_USE_TEMP_DB_FOR_AUTOGEN = True

if _USE_TEMP_DB_FOR_AUTOGEN:
    _DB_URL: str = settings.mysql_message_url.replace(
        "BiliMessageDB", "BiliMessageDB_temp"
    )
else:
    _DB_URL: str = settings.mysql_message_url

config = context.config
config.set_main_option("sqlalchemy.url", _DB_URL)


def do_run_migrations(connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    connectable = create_async_engine(_DB_URL)
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_offline() -> None:
    context.configure(
        url=settings.mysql_sync_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
