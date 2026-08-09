"""MySQL 异步引擎与会话管理。

message-service 的持久化统一走 MySQL（本项目不使用 Redis，所有数据直接落 MySQL）：

- 主库（元数据库）：系统通知、事件提醒、私信索引 / 会话、消息设置、用户活跃度等，
  由 SQLModel 定义表结构、Alembic 管理版本。
- 私信内容库：按月分库 + 库内分表 100 张，表结构由 app.core.sharding 用原生 DDL
  懒创建（Alembic 不接管，避免迁移脚本随月份无限膨胀）。

两者位于同一个 MySQL 实例，因此复用同一个 engine：跨库访问时使用
`库名.表名` 全限定名即可，无需为每个月度库单独建连接池（小设备友好）。
"""

from collections.abc import AsyncGenerator
from typing import Annotated

from fastapi import Depends
from loguru import logger
from sqlalchemy import NullPool, text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlmodel.ext.asyncio.session import AsyncSession
from app.core.config import settings

_connect_args: dict = {"charset": "utf8mb4", "autocommit": False}

engine = create_async_engine(
    url=settings.mysql_message_url,
    pool_size=settings.mysql_pool_size,
    max_overflow=settings.mysql_max_overflow,
    # 取连接前先 ping，避免拿到被 MySQL wait_timeout 静默断开的陈旧连接
    pool_pre_ping=True,
    # 回收周期必须小于 MySQL wait_timeout(默认 600s)
    pool_recycle=settings.mysql_pool_recycle,
    pool_timeout=30,
    echo=settings.mysql_echo,
    future=True,
    connect_args=_connect_args,
)

async_session_maker: async_sessionmaker[AsyncSession] = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
)


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI 依赖：获取一个自动关闭的数据库会话。"""
    async with async_session_maker() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise


# 路由函数签名中直接使用的依赖注解类型
SessionDep = Annotated[AsyncSession, Depends(get_session)]


def new_session() -> AsyncSession:
    """在非请求上下文（MQ 消费者 / 定时任务）中手动创建会话。

    调用方需自行使用 `async with` 管理生命周期。
    """
    return async_session_maker()


# ==================== pptr Postgres（由 be-message 接管）====================
# 直连 be-gateway 的 Postgres（PPTR_Bili_Lot）。
# 历史：早期仅用于读取用户展示信息与 @ 搜索，表结构由 pptr 的 sequelize 迁移管理，
#       本服务用独立 metadata 的只读 SQLModel 表达、不纳入 Alembic。
# 现状：be-message 已**彻底接管**该库（可读可写），完整表结构统一在
#       app.models.pptr_db（挂 SQLModel.metadata），由独立 Alembic 分支
#       alembic_pptr/ 以当前库为 baseline 接管版本演进。

pptr_engine = create_async_engine(
    url=settings.postgres_pptr_url,
    pool_size=settings.postgres_pptr_pool_size,
    max_overflow=settings.postgres_pptr_max_overflow,
    pool_pre_ping=True,
    pool_recycle=settings.postgres_pptr_pool_recycle,
    pool_timeout=30,
    echo=settings.postgres_pptr_echo,
    future=True,
)

pptr_session_maker = async_sessionmaker(
    bind=pptr_engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
)


async def get_pptr_session() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI 依赖：获取一个 pptr Postgres 会话（由 be-message 接管，可读写）。"""
    async with pptr_session_maker() as session:
        yield session


# 路由函数签名中直接使用的依赖注解类型
PptrSessionDep = Annotated[AsyncSession, Depends(get_pptr_session)]


def new_pptr_session() -> AsyncSession:
    """在非请求上下文（服务层内部批量读 / 接管后亦可写）中手动创建 pptr 会话。

    调用方需自行使用 `async with` 管理生命周期；读取无需显式 commit，写入需 commit。
    """
    return pptr_session_maker()


async def ensure_database() -> bool:
    """确保主库存在，不存在则自动创建。

    Alembic 只能管理「库内的表」，库本身必须先存在；容器首次启动时
    `BiliMessageDB` 往往还没建，这里先用一个不带库名的连接把它建出来，
    避免运维手工执行 `CREATE DATABASE`。
    """
    url = make_url(settings.mysql_message_url)
    db_name = url.database
    if not db_name:
        logger.error("MYSQL_MESSAGE_URL 中未指定数据库名")
        return False

    # 连到 MySQL 实例本身（不指定库）执行建库语句
    # URL.set() 会忽略 None，因此这里显式用 URL.create 重建一个不含 database 的连接串
    server_url = URL.create(
        drivername=url.drivername,
        username=url.username,
        password=url.password,
        host=url.host,
        port=url.port,
        query=url.query,
    )
    server_engine = create_async_engine(server_url, poolclass=NullPool)
    try:
        async with server_engine.begin() as conn:
            await conn.execute(
                text(
                    f"CREATE DATABASE IF NOT EXISTS `{db_name}` "
                    "DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
                )
            )
        logger.info(f"消息系统主库 {db_name} 已就绪")
        return True
    except Exception as e:  # noqa: BLE001
        logger.error(f"创建主库 {db_name} 失败: {e}")
        return False
    finally:
        await server_engine.dispose()


async def test_connection() -> bool:
    """探测 MySQL 连通性，供启动自检使用。"""
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return True
    except Exception as e:  # noqa: BLE001
        logger.error(f"MySQL 连接测试失败: {e}")
        return False


async def test_pptr_connection() -> bool:
    """探测 pptr Postgres 连通性，供启动自检使用。"""
    try:
        async with pptr_engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return True
    except Exception as e:  # noqa: BLE001
        logger.error(f"pptr Postgres 连接测试失败: {e}")
        return False


__all__ = [
    "engine",
    "async_session_maker",
    "get_session",
    "SessionDep",
    "new_session",
    "ensure_database",
    "test_connection",
    # pptr Postgres（由 be-message 接管）
    "pptr_engine",
    "pptr_session_maker",
    "get_pptr_session",
    "PptrSessionDep",
    "new_pptr_session",
    "test_pptr_connection",
]
