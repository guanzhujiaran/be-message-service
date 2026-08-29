"""StrInt 路由参数归一化回归测试（2026-08-29）。

`StrInt = Annotated[Union[int, str], BeforeValidator(...)]` 用于雪花 ID 入参，
但 **FastAPI 只在 `Annotated[StrInt, Query(...)]` 形式下才会应用 BeforeValidator**：
写成 `x: StrInt = Query(...)`（默认值形式）时元数据被丢弃，字符串入参原样透传
给 handler，`if mid <= 0` 直接抛
`TypeError: '<=' not supported between instances of 'str' and 'int'` → 500。

HTTP 查询参数在线上永远是字符串，因此这类路由**任何调用都会 500**。本用例用
字符串查询参数直打真实路由，守住这条回归。

说明：用 `ASGITransport` 而非 `TestClient`——后者会在**另一个事件循环**（portal
线程）里跑 app，与本测试绑定的异步引擎不在同一 loop，SQLAlchemy 会报
「Future attached to a different loop」。

详见 `app/models/str_int.py` 模块说明。
"""

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlmodel.ext.asyncio.session import AsyncSession as SQLModelAsyncSession

from app.core import database as db_mod
from app.core.config import settings
from app.main import app

# 受影响的 StrInt 查询参数路由（公开接口，无需登录态；2.52.0 已删 /follow/stat 与 /upstat）
_STR_INT_QUERY_URLS = [
    "/api/v1/user/space/info?mid=127763472",
]


@pytest.fixture(autouse=True)
async def _bind_engine_per_test():
    """为当前事件循环重建引擎（app 与测试共用同一 loop，避免跨 loop 报错）。"""
    engine = create_async_engine(
        settings.mysql_message_url,
        pool_pre_ping=True,
        future=True,
        connect_args={"charset": "utf8mb4", "autocommit": False},
    )
    db_mod.engine = engine
    db_mod.async_session_maker = async_sessionmaker(
        bind=engine,
        class_=SQLModelAsyncSession,
        expire_on_commit=False,
        autoflush=False,
    )
    pptr_engine = create_async_engine(
        url=settings.postgres_pptr_url,
        pool_pre_ping=True,
        future=True,
    )
    db_mod.pptr_engine = pptr_engine
    db_mod.pptr_session_maker = async_sessionmaker(
        bind=pptr_engine,
        class_=SQLModelAsyncSession,
        expire_on_commit=False,
        autoflush=False,
    )
    yield
    await engine.dispose()
    await pptr_engine.dispose()


@pytest.mark.parametrize("url", _STR_INT_QUERY_URLS)
async def test_str_int_query_param_coerced_to_int(url: str) -> None:
    """字符串雪花 ID 入参必须被归一为 int，handler 不得因类型比较抛 500。"""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.get(url)
    assert resp.status_code != 500, (
        f"{url} 触发 500（StrInt 未归一为 int）: {resp.text[:300]}"
    )
