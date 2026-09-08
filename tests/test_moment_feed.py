"""Phase 3 — 动态 Feed / 详情服务单元测试（P3-T7）。

覆盖：

- 综合页：仅 normal + 未软删 + pubTime 非空，按 pubTime 倒序。
- 空间页：本人视角含 auditing/rejected；访客视角仅 normal；置顶优先。
- 详情权限：非作者看 auditing → 不可见（None）；作者看 auditing → 可见。
- 批量详情：过滤已软删 / 非 normal 非作者。

测试用独立 mid / dynId 区间，避免与既有用例互相干扰。
"""

import datetime

import pytest
from sqlmodel import text

from app.core.database import new_session
from app.core.sharding import generate_moment_id
from app.models.db import TMoment
from app.models.enums import (
    ResourceAuditStatusEnum,
    MomentTypeEnum,
)
from app.services.moment.moment_feed import MomentFeedService
from seed_biliopus import RealDyn, fetch_real_dyns

D_MID = 920001
D_MID2 = 920002


async def _new_moment_id() -> int:
    return await generate_moment_id()


# 时间基准用当前本地时间（CST）：避免 seed 固定在久远过去而被库中已存在的大量
# normal 动态（如灌数数据）挤出综合 Feed 第一页，导致「库空」假设的断言失效。
# 与业务写入（datetime.now()）同一基准。
_BASE = datetime.datetime.now()  # noqa: DTZ005


async def _seed(
    session,
    mid: int,
    *,
    real: RealDyn | None = None,
    dyn_type: MomentTypeEnum = MomentTypeEnum.WORD,
    audit: ResourceAuditStatusEnum = ResourceAuditStatusEnum.NORMAL,
    is_top: int = 0,
    deleted: bool = False,
    minutes_ago: int = 0,
    repost_src: int | None = None,
) -> int:
    did = await _new_moment_id()
    now = _BASE - datetime.timedelta(minutes=minutes_ago)
    content_text = real.content_text if real else "feed seed"
    dyn = TMoment(
        dynId=did,
        mid=mid,
        dynType=dyn_type,
        contentText=content_text,
        contentJson=[{"type": "WORDS", "text": content_text}],
        repostSrcDynId=repost_src,
        auditStatus=audit,
        pubTime=now if audit is ResourceAuditStatusEnum.NORMAL else None,
        isTop=is_top,
        deletedAt=(now if deleted else None),
        created_at=now,
        updated_at=now,
    )
    session.add(dyn)
    return did


async def _commit_seed(session, mid, **kw) -> int:
    did = await _seed(session, mid, **kw)
    await session.flush()
    await session.commit()
    return did


async def _real_dyns(n: int) -> list[RealDyn]:
    """从 biliopusdb 拉取 n 条真实动态作为测试种子内容。"""
    return await fetch_real_dyns(n)


async def _cleanup(dids: list[int]) -> None:
    async with new_session() as s:
        for d in dids:
            await s.exec(text(f"DELETE FROM TResourceAuditLog WHERE bizType = 1 AND bizId = {d}"))
            await s.exec(text(f"DELETE FROM TMoment WHERE dynId = {d}"))
        await s.commit()


async def _cleanup_all(dids: list[int]) -> None:
    await _cleanup(dids)


@pytest.fixture(autouse=True)
async def _bind_engine_per_test():
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlmodel.ext.asyncio.session import AsyncSession as SQLModelAsyncSession

    from app.core import database as db_mod
    from app.core.config import settings

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
    # pptr engine 也重建并绑定当前事件循环（feed 渲染需回查 pptr 用户，
    # 模块级单例 engine 会绑到首个 loop，跨 loop 复用会报 "attached to a different loop"）
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


async def test_comprehensive_only_normal():
    dids = []
    reals = await _real_dyns(4)
    async with new_session() as s:
        dids.append(await _commit_seed(s, D_MID, real=reals[0], audit=ResourceAuditStatusEnum.NORMAL, minutes_ago=10))
        dids.append(await _commit_seed(s, D_MID, real=reals[1], audit=ResourceAuditStatusEnum.AUDITING, minutes_ago=9))
        dids.append(await _commit_seed(s, D_MID, real=reals[2], audit=ResourceAuditStatusEnum.REJECTED, minutes_ago=8))
        dids.append(await _commit_seed(s, D_MID, real=reals[3], audit=ResourceAuditStatusEnum.NORMAL, minutes_ago=7, deleted=True))
    try:
        async with new_session() as s:
            # 2.27.0：/feed/all 默认 recommend（EdgeRank，72h 候选窗口），
            # 本测试关心 normal 过滤，显式 sort="time" 保持原语义。
            # 库中可能存在其它 normal 数据（灌数），故断言「seed 的 normal 动态
            # 在返回中、auditing/rejected/已软删不在、返回项全部 normal」。
            resp = await MomentFeedService.comprehensive_feed(
                s, page=1, page_size=50, sort="time"
            )
            ids = {it.dynId for it in resp.items}
            assert dids[0] in ids  # normal 动态可见
            assert not ({dids[1], dids[2], dids[3]} & ids)  # auditing/rejected/软删不可见
            # 装配返回 auditStatus 为枚举成员名字符串（大写），与 MomentFeedItem 字段类型一致
            assert all(it.auditStatus == "NORMAL" for it in resp.items)
    finally:
        await _cleanup_all(dids)


async def test_comprehensive_order_by_pubtime():
    dids = []
    reals = await _real_dyns(3)
    async with new_session() as s:
        dids.append(await _commit_seed(s, D_MID, real=reals[0], audit=ResourceAuditStatusEnum.NORMAL, minutes_ago=30))
        dids.append(await _commit_seed(s, D_MID, real=reals[1], audit=ResourceAuditStatusEnum.NORMAL, minutes_ago=20))
        dids.append(await _commit_seed(s, D_MID, real=reals[2], audit=ResourceAuditStatusEnum.NORMAL, minutes_ago=10))
    try:
        async with new_session() as s:
            # 2.27.0：本测试验证 pubTime 倒序，显式 sort="time"（默认已变 recommend）
            resp = await MomentFeedService.comprehensive_feed(
                s, page=1, page_size=20, sort="time"
            )
            times = [it.pubTime for it in resp.items]
            assert times == sorted(times, reverse=True)
    finally:
        await _cleanup_all(dids)


async def test_space_self_sees_all_states():
    dids = []
    reals = await _real_dyns(3)
    async with new_session() as s:
        dids.append(await _commit_seed(s, D_MID, real=reals[0], audit=ResourceAuditStatusEnum.NORMAL, minutes_ago=10))
        dids.append(await _commit_seed(s, D_MID, real=reals[1], audit=ResourceAuditStatusEnum.AUDITING, minutes_ago=9))
        dids.append(await _commit_seed(s, D_MID, real=reals[2], audit=ResourceAuditStatusEnum.REJECTED, minutes_ago=8))
    try:
        async with new_session() as s:
            resp = await MomentFeedService.space_feed(s, host_mid=D_MID, viewer_mid=D_MID)
            statuses = {it.auditStatus for it in resp.items}
            # 装配返回 auditStatus 为枚举成员名字符串（大写）
            assert statuses == {"NORMAL", "AUDITING", "REJECTED"}
    finally:
        await _cleanup_all(dids)


async def test_space_visitor_only_normal():
    dids = []
    reals = await _real_dyns(2)
    async with new_session() as s:
        dids.append(await _commit_seed(s, D_MID, real=reals[0], audit=ResourceAuditStatusEnum.NORMAL, minutes_ago=10))
        dids.append(await _commit_seed(s, D_MID, real=reals[1], audit=ResourceAuditStatusEnum.AUDITING, minutes_ago=9))
    try:
        async with new_session() as s:
            resp = await MomentFeedService.space_feed(s, host_mid=D_MID, viewer_mid=D_MID2)
            assert all(it.auditStatus == "NORMAL" for it in resp.items)
    finally:
        await _cleanup_all(dids)


async def test_space_top_priority():
    dids = []
    reals = await _real_dyns(2)
    async with new_session() as s:
        dids.append(await _commit_seed(s, D_MID, real=reals[0], audit=ResourceAuditStatusEnum.NORMAL, minutes_ago=10))
        dids.append(await _commit_seed(s, D_MID, real=reals[1], audit=ResourceAuditStatusEnum.NORMAL, minutes_ago=5, is_top=1))
    try:
        async with new_session() as s:
            resp = await MomentFeedService.space_feed(s, host_mid=D_MID, viewer_mid=D_MID)
            assert resp.items[0].isTop == 1
    finally:
        await _cleanup_all(dids)


async def test_detail_visitor_cannot_see_auditing():
    dids = []
    reals = await _real_dyns(1)
    async with new_session() as s:
        dids.append(await _commit_seed(s, D_MID, real=reals[0], audit=ResourceAuditStatusEnum.AUDITING, minutes_ago=10))
    try:
        async with new_session() as s:
            d = await MomentFeedService.get_detail(s, dids[0], viewer_mid=D_MID2)
            assert d is None
            d2 = await MomentFeedService.get_detail(s, dids[0], viewer_mid=D_MID)
            assert d2 is not None and d2.auditStatus == "AUDITING"
    finally:
        await _cleanup_all(dids)


async def test_detail_normal_visible_to_all():
    dids = []
    reals = await _real_dyns(1)
    async with new_session() as s:
        dids.append(await _commit_seed(s, D_MID, real=reals[0], audit=ResourceAuditStatusEnum.NORMAL, minutes_ago=10))
    try:
        async with new_session() as s:
            d = await MomentFeedService.get_detail(s, dids[0], viewer_mid=D_MID2)
            assert d is not None and d.auditStatus == "NORMAL"
    finally:
        await _cleanup_all(dids)


async def test_batch_filters_non_normal_non_author():
    dids = []
    reals = await _real_dyns(3)
    async with new_session() as s:
        dids.append(await _commit_seed(s, D_MID, real=reals[0], audit=ResourceAuditStatusEnum.NORMAL, minutes_ago=10))
        dids.append(await _commit_seed(s, D_MID, real=reals[1], audit=ResourceAuditStatusEnum.AUDITING, minutes_ago=9))
        dids.append(await _commit_seed(s, D_MID, real=reals[2], audit=ResourceAuditStatusEnum.NORMAL, minutes_ago=8, deleted=True))
    try:
        async with new_session() as s:
            resp = await MomentFeedService.get_details_batch(s, dids, viewer_mid=D_MID2)
            got = {it.dynId for it in resp}
            # 访客：仅 normal 且未删的可见
            assert got == {dids[0]}
            # 作者本人：auditing 也可见（未删）
            resp2 = await MomentFeedService.get_details_batch(s, dids, viewer_mid=D_MID)
            got2 = {it.dynId for it in resp2}
            assert got2 == {dids[0], dids[1]}
    finally:
        await _cleanup_all(dids)


__all__ = [
    "test_batch_filters_non_normal_non_author",
    "test_comprehensive_only_normal",
    "test_comprehensive_order_by_pubtime",
    "test_detail_normal_visible_to_all",
    "test_detail_visitor_cannot_see_auditing",
    "test_space_self_sees_all_states",
    "test_space_top_priority",
    "test_space_visitor_only_normal",
]
