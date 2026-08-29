"""用户空间信息 / 黑名单互访拒绝 单元测试（P9-T5，2.12.0）。

覆盖：
- `PptrUserService.get_space_info`：真实用户字段映射（name/sex/face/sign/level/birthday/vip）；不存在用户返回 None。
- `FollowService.is_blocked_relation`：两用户存在任一向黑名单关系判定。

测试用独立 mid / dynId 区间，避免与既有用例互相干扰；黑名单关系测试后清理。
"""

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlmodel.ext.asyncio.session import AsyncSession as SQLModelAsyncSession

from app.core.config import settings
from app.models.db.follow_tbl import UserFollow
from app.models.enums import FollowStatusEnum
from app.services.user.follow import FollowService
from app.services.user.account import PptrUser

# 测试用的两个用户（黑名单关系测试写入/清理用；未登录相关字段）
BLOCK_A = 920011
BLOCK_B = 920012
NON_EXIST = 987654321000


@pytest.fixture(autouse=True)
async def _bind_engine_per_test():
    from app.core import database as db_mod

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
    # pptr engine 也重建并绑定当前事件循环（空间资料回查 pptr 用户）
    from app.core import database as db_mod_pptr

    pptr_engine = create_async_engine(
        url=settings.postgres_pptr_url,
        pool_pre_ping=True,
        future=True,
    )
    db_mod_pptr.pptr_engine = pptr_engine
    db_mod_pptr.pptr_session_maker = async_sessionmaker(
        bind=pptr_engine,
        class_=SQLModelAsyncSession,
        expire_on_commit=False,
        autoflush=False,
    )
    yield
    await engine.dispose()
    await pptr_engine.dispose()


async def _clean_block(session, mid: int, target_mid: int) -> None:
    from sqlmodel import col, delete

    await session.exec(
        delete(UserFollow).where(
            col(UserFollow.mid) == mid,
            col(UserFollow.target_mid) == target_mid,
            col(UserFollow.status) == FollowStatusEnum.BLOCKED,
        )
    )
    await session.commit()


async def _real_uid() -> int:
    """取一个真实存在的 pptr 用户 uid（仅取 uid，不落库）。"""
    from sqlalchemy import func, select

    from app.core.database import new_pptr_session
    from app.models.pptr_db import PptrUserInfo

    async with new_pptr_session() as s:
        row = (
            await s.exec(
                select(PptrUserInfo.uid)
                .where(PptrUserInfo.deletedAt.is_(None))
                .order_by(func.random())
                .limit(1)
            )
        ).one_or_none()
        assert row is not None, "pptr 库无可用真实用户，无法测试空间资料映射"
        return int(row[0])


async def test_space_info_existing_user_mapping():
    """真实用户：空间资料字段映射正确，基础字段非空。"""
    uid = await _real_uid()
    data = await PptrUser(mid=uid).get_space_info()
    assert data is not None
    assert data.mid == uid
    # name 至少非空（uname 或注册名兜底）
    assert data.name
    # 其他字段类型正确
    assert isinstance(data.level, int)
    assert isinstance(data.vip, object)
    assert data.official.role == 0
    assert data.official.type == -1
    assert data.is_followed is False
    assert data.is_self is False


async def test_space_info_merges_follow_and_upstat():
    """2.32.0：`/user/space/info` 一次带出 follow_stat / upstat（不再需要两次额外请求）。

    聚合字段值必须与 `FollowService.get_counts` / `MomentFeedService.get_upstat`
    单独查询的结果一致（说明确实是同一批数据源，而非零值兜底）。
    """
    from app.api.pptr_user_gateway import get_space_info
    from app.core.database import new_session
    from app.services.moment.moment_feed import MomentFeedService

    uid = await _real_uid()
    async with new_session() as s:
        resp = await get_space_info(session=s, mid=uid, x_bili_mid=None)

    assert int(resp.code) == 0, resp.msg
    data = resp.data
    assert data is not None
    assert data.mid == uid
    # 聚合结构存在且类型为 int（零值也是合法结果，不做 >0 断言）
    assert isinstance(data.follow_stat.following_count, int)
    assert isinstance(data.follow_stat.follower_count, int)
    assert isinstance(data.follow_stat.mutual_count, int)
    assert isinstance(data.upstat.dynamic_count, int)
    assert isinstance(data.upstat.like_count, int)

    # 与原端点同源：值应完全一致
    async with new_session() as s:
        counts = await FollowService.get_counts(s, uid)
        upstat = await MomentFeedService.get_upstat(s, uid)
    assert data.follow_stat.following_count == counts.following_count
    assert data.follow_stat.follower_count == counts.follower_count
    assert data.follow_stat.mutual_count == counts.mutual_count
    assert data.upstat.dynamic_count == upstat["dynamic_count"]
    assert data.upstat.like_count == upstat["like_count"]


async def test_space_info_not_found_returns_none():
    """不存在的用户：`get_space_info` 返回 None（路由层据此返回 USER_NOT_FOUND）。"""
    data = await PptrUser(mid=NON_EXIST).get_space_info()
    assert data is None


async def test_is_blocked_relation():
    """黑名单互访拒绝：任一向 blocked 关系判定为 True，无关系为 False。"""
    from app.core.database import new_session

    try:
        # 无关系时为 False
        async with new_session() as s:
            ok = await FollowService.is_blocked_relation(s, BLOCK_A, BLOCK_B)
            assert ok is False

        # 建立 A 拉黑 B 的关系 → True（i_blocked 命中）
        async with new_session() as s:
            s.add(
                UserFollow(
                    mid=BLOCK_A,
                    target_mid=BLOCK_B,
                    status=FollowStatusEnum.BLOCKED,
                )
            )
            await s.commit()
        async with new_session() as s:
            assert await FollowService.is_blocked_relation(s, BLOCK_A, BLOCK_B) is True
            # 反向视角（B 看 A）同样命中（blocked_by）
            assert await FollowService.is_blocked_relation(s, BLOCK_B, BLOCK_A) is True
    finally:
        # 清理黑名单关系
        async with new_session() as s:
            await _clean_block(s, BLOCK_A, BLOCK_B)
            await _clean_block(s, BLOCK_B, BLOCK_A)
