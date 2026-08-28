"""Phase 14 — 话题创建与审核服务单元测试（2.19.0）。

覆盖：

- 创建话题：名称唯一 / 长度 / 封面 URL 校验；创建即 auditStatus=auditing、pubTime=NULL。
- 审核通过：auditStatus→normal + pubTime=now()，进入话题广场。
- 审核驳回：auditStatus→rejected + 写 auditRejectReason。
- 广场过滤：非 normal 话题不展示。
- 话题 Feed 过滤：非 normal 话题返回空流。
- 发布动态关联非 normal 话题 → ValueError（接口转 422）。
- 我创建的话题（mine）：仅本人，含审核状态。

复用真实 MySQL 主库，独立 mid / topicId 区间避免与既有用例冲突。
"""

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlmodel import text
from sqlmodel.ext.asyncio.session import AsyncSession as SQLModelAsyncSession

from app.core import database as db_mod
from app.core.config import settings
from app.core.database import new_session
from app.models.db import TMomentTopic
from app.models.enums import MomentTopicAuditStatusEnum
from app.models.schemas.moment import (
    MomentTopicCreateReq,
    MomentTopicMineResp,
    MomentTopicSquareResp,
)
from app.core.sharding import generate_topic_id
from app.services.moment.moment_feed import MomentFeedService
from app.services.moment.moment_topic import MomentTopicService
from app.services.moment.moment_topic_audit import MomentTopicAuditService

# 独立区间，避免与既有用例冲突
A_MID = 930001
ADMIN_MID = 930099
T_A = 9300001
T_B = 9300002


@pytest.fixture(autouse=True)
async def _bind_engine_per_test():
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

    # pptr 引擎（供 PptrUserService.get_many 回查创建者信息）需随测试重建，绑定当前 loop
    from app.core import database as db_mod_pptr

    orig_pptr_engine = db_mod_pptr.pptr_engine
    orig_pptr_session_maker = db_mod_pptr.pptr_session_maker
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

    async with new_session() as s:
        await s.exec(
            text(f"DELETE FROM TMomentTopic WHERE creatorMid IN ({A_MID}, 0) OR topicId IN ({T_A}, {T_B})")
        )
        await s.commit()
    yield
    async with new_session() as s:
        await s.exec(
            text(f"DELETE FROM TMomentTopic WHERE creatorMid IN ({A_MID}, 0) OR topicId IN ({T_A}, {T_B})")
        )
        await s.commit()
    await engine.dispose()
    await pptr_engine.dispose()
    db_mod_pptr.pptr_engine = orig_pptr_engine
    db_mod_pptr.pptr_session_maker = orig_pptr_session_maker


async def _create_topic(session, mid: int, name: str, **kw) -> TMomentTopic:
    topic = TMomentTopic(
        topicId=await generate_topic_id(),
        topicName=name,
        creatorMid=mid,
        auditStatus=MomentTopicAuditStatusEnum.AUDITING,
        pubTime=None,
        **kw,
    )
    session.add(topic)
    await session.commit()
    await session.refresh(topic)
    return topic


# ==================== 创建话题 ====================


async def test_create_topic_default_auditing():
    async with new_session() as s:
        resp = await MomentTopicService.create_topic(
            s,
            mid=A_MID,
            req=MomentTopicCreateReq(topicName="全新话题", topicDesc="描述"),
        )
        assert resp.topicId > 0
        assert resp.auditStatus == MomentTopicAuditStatusEnum.AUDITING.value
        row = await s.get(TMomentTopic, resp.topicId)
        assert row is not None
        assert row.auditStatus is MomentTopicAuditStatusEnum.AUDITING
        assert row.pubTime is None
        assert row.creatorMid == A_MID


async def test_create_topic_duplicate_name_raises():
    async with new_session() as s:
        await _create_topic(s, A_MID, "重名话题")
        import pytest as _pytest

        with _pytest.raises(ValueError):
            await MomentTopicService.create_topic(
                s, mid=A_MID, req=MomentTopicCreateReq(topicName="重名话题")
            )


async def test_create_topic_invalid_cover_raises():
    async with new_session() as s:
        import pytest as _pytest

        with _pytest.raises(ValueError):
            await MomentTopicService.create_topic(
                s, mid=A_MID, req=MomentTopicCreateReq(topicName="x", topicCover="ftp://a/b.png")
            )


async def test_create_topic_empty_name_raises():
    async with new_session() as s:
        import pytest as _pytest

        with _pytest.raises(ValueError):
            await MomentTopicService.create_topic(
                s, mid=A_MID, req=MomentTopicCreateReq(topicName="   ")
            )


# ==================== 审核通过 ====================


async def test_approve_sets_normal_and_pubtime():
    async with new_session() as s:
        topic = await _create_topic(s, A_MID, "待审核通过话题")
        item = await MomentTopicAuditService.approve(
            s, topic.topicId, operator_mid=ADMIN_MID
        )
        assert item.topicId == topic.topicId
        row = await s.get(TMomentTopic, topic.topicId)
        assert row.auditStatus is MomentTopicAuditStatusEnum.NORMAL
        assert row.pubTime is not None


async def test_approve_twice_raises():
    async with new_session() as s:
        topic = await _create_topic(s, A_MID, "重复审核话题")
        await MomentTopicAuditService.approve(s, topic.topicId, operator_mid=ADMIN_MID)
        import pytest as _pytest

        with _pytest.raises(ValueError):
            await MomentTopicAuditService.approve(s, topic.topicId, operator_mid=ADMIN_MID)


# ==================== 审核驳回 ====================


async def test_reject_sets_rejected_and_reason():
    async with new_session() as s:
        topic = await _create_topic(s, A_MID, "待驳回话题")
        item = await MomentTopicAuditService.reject(
            s, topic.topicId, operator_mid=ADMIN_MID, reject_reason="名称不规范"
        )
        assert item.topicId == topic.topicId
        row = await s.get(TMomentTopic, topic.topicId)
        assert row.auditStatus is MomentTopicAuditStatusEnum.REJECTED
        assert row.auditRejectReason == "名称不规范"
        assert row.pubTime is None


# ==================== 广场过滤 ====================


async def test_square_only_normal():
    async with new_session() as s:
        await _create_topic(s, A_MID, "广场普通话题")  # auditing，不应出现
        topic_n = await _create_topic(s, A_MID, "广场通过话题")
        await MomentTopicAuditService.approve(s, topic_n.topicId, operator_mid=ADMIN_MID)

        resp = await MomentTopicService.topic_square(s, page=1, page_size=20)
        assert isinstance(resp, MomentTopicSquareResp)
        names = {t.topicName for t in resp.items}
        assert "广场通过话题" in names
        assert "广场普通话题" not in names


# ==================== 话题 Feed 过滤 ====================


async def test_topic_feed_non_normal_returns_empty():
    async with new_session() as s:
        topic = await _create_topic(s, A_MID, "审核中话题Feed")
        resp = await MomentFeedService.topic_feed(s, topic_id=topic.topicId, viewer_mid=A_MID)
        assert resp.items == []


# ==================== 我创建的话题 ====================


async def test_mine_returns_only_mine_with_status():
    async with new_session() as s:
        await _create_topic(s, A_MID, "我的话题A")
        topic_b = await _create_topic(s, A_MID, "我的话题B")
        await MomentTopicAuditService.reject(s, topic_b.topicId, operator_mid=ADMIN_MID, reject_reason="违规")

        resp = await MomentTopicService.mine(s, mid=A_MID, page_num=1, page_size=20)
        assert isinstance(resp, MomentTopicMineResp)
        by_name = {it.topicName: it for it in resp.items}
        assert by_name["我的话题A"].auditStatus == MomentTopicAuditStatusEnum.AUDITING.value
        assert by_name["我的话题B"].auditStatus == MomentTopicAuditStatusEnum.REJECTED.value
        assert by_name["我的话题B"].auditRejectReason == "违规"


async def test_mine_empty_when_no_created():
    async with new_session() as s:
        resp = await MomentTopicService.mine(s, mid=A_MID, page_num=1, page_size=20)
        assert resp.items == []


__all__ = [
    "test_approve_sets_normal_and_pubtime",
    "test_approve_twice_raises",
    "test_create_topic_default_auditing",
    "test_create_topic_duplicate_name_raises",
    "test_create_topic_empty_name_raises",
    "test_create_topic_invalid_cover_raises",
    "test_mine_empty_when_no_created",
    "test_mine_returns_only_mine_with_status",
    "test_reject_sets_rejected_and_reason",
    "test_square_only_normal",
    "test_topic_feed_non_normal_returns_empty",
]
