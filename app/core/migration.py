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

from alembic.config import Config as AlembicConfig
from alembic.script import ScriptDirectory
from loguru import logger
from sqlalchemy import Connection, create_engine, inspect, text
from sqlalchemy.pool import NullPool

from alembic import command
from app.core.config import settings

# 项目根目录（be-message-service/），alembic.ini 位于此处
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# 主库版本链压缩后的基线 revision（空 DDL，表达「库已是生产结构」这一既成事实）。
# 见 docs/be-message-统一计划书.md §5.28。
MESSAGE_BASELINE_REVISION = "20261011_msg_prod_baseline"
MESSAGE_SYNC_REVISION = "sync_to_prod"

# sync_to_prod 的可观测结果：建了这 5 张表、删了 TUserFavoriteSetting；
# 20261013_msg_push_toggles 给 msg_user_setting 加了 3 个 push 列。
# 用于「版本号丢失」时反推库实际处在版本链的哪一环。
_SYNC_CREATED_TABLES = frozenset(
    {
        "TFeedBanditArm",
        "TFeedEventLog",
        "TUserSpacePrivacy",
        "msg_user_deactivation",
        "msg_user_follow_tag",
    }
)
_SYNC_DROPPED_TABLE = "TUserFavoriteSetting"
_PUSH_TOGGLE_TABLE = "msg_user_setting"
_PUSH_TOGGLE_COLUMNS = frozenset({"push_reply", "push_at", "push_like"})


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


def _resolve_stamp_target(conn: Connection, head: str | None) -> str:
    """按库里**真实落地的结构**反推该 stamp 到哪个 revision。

    只认 sync 迁移的可见结果（建表 / 删表 / 加列），不猜版本号：
    - sync 的表一个都没有 → 库就是基线状态，stamp 基线，由 upgrade 补建；
    - sync 的表都在且 `TUserFavoriteSetting` 已删 → sync 已落地，
      再看 `msg_user_setting` 的三个 push 列在不在，决定 stamp sync 还是 head。
    """
    tables = set(inspect(conn).get_table_names())
    if not _SYNC_CREATED_TABLES.issubset(tables) or _SYNC_DROPPED_TABLE in tables:
        return MESSAGE_BASELINE_REVISION
    columns = {col["name"] for col in inspect(conn).get_columns(_PUSH_TOGGLE_TABLE)}
    if head and _PUSH_TOGGLE_COLUMNS.issubset(columns):
        return head
    return MESSAGE_SYNC_REVISION


def _repair_dropped_revision(cfg: AlembicConfig) -> None:
    """存量库的 `alembic_version` 若记录了已被压缩删除的旧 revision，改写到应有版本。

    §5.28 把主库版本链压缩成「基线 + sync」两条后，旧链的 revision id
    （如 `20260920_stat_viewlog_enum`）已不在 `alembic/versions/` 里。此时 alembic
    解析当前版本就会抛 `Can't locate revision identified by ...`，**`upgrade` /
    `stamp` / `current` 无一幸免**（stamp 同样要先解析当前版本），
    因此只能绕过 CLI 直接改 `alembic_version.version_num`，
    语义等价于手册里的 `alembic stamp <版本>`。
    """
    script = ScriptDirectory.from_config(cfg)
    known = {rev.revision for rev in script.walk_revisions()}
    head = script.get_current_head()
    engine = create_engine(settings.mysql_sync_url, poolclass=NullPool)
    try:
        with engine.begin() as conn:
            if "alembic_version" not in inspect(conn).get_table_names():
                # 全新空库：交给 alembic 自己从 <base> 走
                return
            rows = [
                row[0]
                for row in conn.execute(text("SELECT version_num FROM alembic_version"))
            ]
            dropped = [rev for rev in rows if rev not in known]
            if not dropped:
                return
            target = _resolve_stamp_target(conn, head)
            logger.warning(
                f"主库 alembic_version 记录的是已删除的旧 revision {dropped}，"
                f"按库内真实结构改写为 {target}"
            )
            conn.execute(text("DELETE FROM alembic_version"))
            conn.execute(
                text("INSERT INTO alembic_version (version_num) VALUES (:rev)"),
                {"rev": target},
            )
    finally:
        engine.dispose()


def _upgrade_message(target: str) -> None:
    """主库迁移主体（同步执行，内部含 env.py 的 asyncio.run）。"""
    cfg = _alembic_config()
    _repair_dropped_revision(cfg)
    command.upgrade(cfg, target)


async def run_alembic_upgrade() -> bool:
    """执行主库（MySQL）alembic upgrade，成功返回 True。"""
    target = settings.alembic_upgrade_target
    logger.info(f"===== 开始执行 alembic upgrade {target}（主库）=====")
    try:
        # env.py 内部会 asyncio.run，需放到独立线程执行，避免与当前事件循环冲突
        await asyncio.to_thread(_upgrade_message, target)
        logger.info(f"===== alembic upgrade {target}（主库）完成 =====")
        return True
    except Exception as e:
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
    except Exception as e:
        logger.error(f"alembic upgrade {target}（pptr 库）失败: {e}")
        return False


__all__ = ["PROJECT_ROOT", "run_alembic_pptr_upgrade", "run_alembic_upgrade"]
