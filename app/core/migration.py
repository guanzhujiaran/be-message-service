"""Alembic 数据库迁移入口。

启动时自动执行 `alembic upgrade head`，把主库 Schema 拉齐到最新版本。
私信内容的月度分库分表不由 Alembic 管理（月份是无限增长的，
写进迁移脚本会导致版本文件无限膨胀），改由 `app.core.sharding` 懒创建。

本服务同时管理两个独立 Alembic 分支：
1. `alembic/`（alembic.ini）：be-message 主库（MySQL 元数据库）；
2. `alembic_pptr/`（alembic_pptr.ini）：pptr Postgres 用户库（PTR_Bili_Lot），
   DB URL 由 env.py 从 settings.postgres_pptr_url 动态注入，不在此硬编码。
"""

import asyncio
import sys
from pathlib import Path

from alembic import command
from alembic.config import Config as AlembicConfig
from loguru import logger

from app.core.config import settings

# 项目根目录（be-message-service/），alembic.ini 位于此处
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def _alembic_config() -> AlembicConfig:
    cfg = AlembicConfig(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", settings.mysql_message_url)
    return cfg


def _alembic_pptr_config() -> AlembicConfig:
    """pptr Postgres 库的 Alembic 配置。

    DB URL 由 alembic_pptr/env.py 从 settings.postgres_pptr_url 动态注入
    （同步驱动版），故此处不再 set sqlalchemy.url，避免与 env.py 冲突。
    """
    cfg = AlembicConfig(str(PROJECT_ROOT / "alembic_pptr.ini"))
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "alembic_pptr"))
    return cfg


async def run_alembic_upgrade() -> bool:
    """执行主库（MySQL）alembic upgrade，成功返回 True。"""
    target = settings.alembic_upgrade_target
    logger.info(f"===== 开始执行 alembic upgrade {target}（主库）=====")
    try:
        # env.py 内部会 asyncio.run，需放到独立线程执行，避免与当前事件循环冲突
        await asyncio.to_thread(command.upgrade, _alembic_config(), target)
        logger.info(f"===== alembic upgrade {target}（主库）完成 =====")
        return True
    except Exception as e:  # noqa: BLE001
        logger.error(f"alembic upgrade {target}（主库）失败: {e}")
        return False


async def run_alembic_pptr_upgrade() -> bool:
    """执行 pptr Postgres 库 alembic upgrade，成功返回 True。"""
    target = settings.alembic_upgrade_target
    logger.info(f"===== 开始执行 alembic upgrade {target}（pptr 库）=====")
    try:
        await asyncio.to_thread(command.upgrade, _alembic_pptr_config(), target)
        logger.info(f"===== alembic upgrade {target}（pptr 库）完成 =====")
        return True
    except Exception as e:  # noqa: BLE001
        logger.error(f"alembic upgrade {target}（pptr 库）失败: {e}")
        return False


__all__ = ["run_alembic_upgrade", "run_alembic_pptr_upgrade", "PROJECT_ROOT"]
