"""Phase 5 — 动态话题 & @ & POI 服务单元测试（P5-T8）。

覆盖：

- 话题广场（P5-T1）：列 TMomentTopic，按 isHot / sortWeight / dynCount 倒序；分页。
- 热门话题（P5-T1 hot_only）：仅返回 isHot=1。
- 话题 Feed（P5-T2）：以 topicId 过滤，仅 normal + 未软删，游标分页。
- @用户推荐（P5-T3）：无关注 / 粉丝时返回空分组（不依赖 pptr 数据）。
- @用户搜索（P5-T4）：空关键词返回空列表；不抛异常。
- POI 附近（P5-T5）：基于已发 Moment 的 lbsPoi 去重聚合。
- POI 关键词搜索（P5-T6）：按 lbsPoi 模糊匹配。

复用真实 MySQL 主库，独立 mid / topicId / dynId 区间避免与既有用例冲突。
"""

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlmodel import text
from sqlmodel.ext.asyncio.session import AsyncSession as SQLModelAsyncSession

from app.core import database as db_mod
from app.core.config import settings
from app.core.database import new_session
from app.core.sharding import generate_moment_id
from app.models.db import TMoment, TMomentTopic, TResourceFeed
from bili_common.models import InteractionBizTypeEnum
from app.models.enums import (
    MomentAuditStatusEnum,
    MomentTopicAuditStatusEnum,
    MomentTypeEnum,
)
from app.models.schemas.moment import (
    MomentAtListResp,
    MomentAtSearchResp,
    MomentPoiResp,
    MomentTopicSquareResp,
)
from app.services.moment.moment_feed import MomentFeedService
from app.services.moment.moment_topic import MomentTopicService
from seed_biliopus import fetch_real_dyns, fetch_real_topics

# 独立区间，避免与 Phase 2 用例（D_MID=910001）冲突
T_MID = 920001
T_MID2 = 920002
T_TOPIC_A = 9200001
T_TOPIC_B = 9200002


@pytest.fixture(autouse=True)
async def _bind_engine_per_test():
    """每个测试在自己的事件循环里重建 engine，并清理上一轮可能残留的测试数据。

    dynId 由雪花算法生成，连续运行（时间桶未推进）可能撞号，故在测试开始前
    按本模块专属 mid / topicId 区间预清理，保证用例幂等可重复执行。
    """
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
    # pptr engine 也绑定当前事件循环：模块级单例绑定首个 loop，跨测试文件/
    # 事件循环复用会报 "attached to a different loop"（与 test_moment_feed 一致）
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
    async with new_session() as s:
        await s.exec(
            text(
                f"DELETE FROM TResourceFeed WHERE bizType = 1 AND bizId IN "
                f"(SELECT dynId FROM TMoment WHERE mid IN ({T_MID}, {T_MID2}))"
            )
        )
        await s.exec(
            text(
                f"DELETE FROM TResourceFeed WHERE bizType = 1 AND bizId IN "
                f"(SELECT dynId FROM TMoment WHERE mid IN ({T_MID}, {T_MID2}))"
            )
        )
        await s.exec(text(f"DELETE FROM TMoment WHERE mid IN ({T_MID}, {T_MID2})"))
        await s.exec(
            text(
                f"DELETE FROM TMomentTopic WHERE topicId IN "
                f"({T_TOPIC_A}, {T_TOPIC_B}, 9200003, 9200004)"
            )
        )
        await s.commit()
    yield
    await engine.dispose()
    await pptr_engine.dispose()


def _new_topic(
    topic_id: int,
    *,
    name: str,
    is_hot: int = 0,
    sort_weight: int = 0,
    dyn_count: int = 0,
    audit_status: MomentTopicAuditStatusEnum = MomentTopicAuditStatusEnum.NORMAL,
) -> TMomentTopic:
    now = __import__("datetime").datetime.now()
    return TMomentTopic(
        topicId=topic_id,
        topicName=name,
        isHot=is_hot,
        sortWeight=sort_weight,
        dynCount=dyn_count,
        creatorMid=0,
        auditStatus=audit_status,
        pubTime=now if audit_status is MomentTopicAuditStatusEnum.NORMAL else None,
    )


async def _seed_topic(session, topic: TMomentTopic) -> None:
    session.add(topic)
    await session.commit()


async def _seed_moment(
    session,
    mid: int,
    *,
    audit_status: MomentAuditStatusEnum = MomentAuditStatusEnum.NORMAL,
    topic_id: int | None = None,
    lbs_poi: str | None = None,
    lbs_lat: float | None = None,
    lbs_lng: float | None = None,
) -> int:
    did = await generate_moment_id()
    now = __import__("datetime").datetime.now()
    reals = await fetch_real_dyns(1)
    content_text = reals[0].content_text if reals else "seed"
    dyn = TMoment(
        dynId=did,
        mid=mid,
        dynType=MomentTypeEnum.WORD,
        contentText=content_text,
        contentJson=[{"type": "WORDS", "text": content_text}],
        topicId=topic_id,
        lbsPoi=lbs_poi,
        lbsLat=lbs_lat,
        lbsLng=lbs_lng,
        auditStatus=audit_status,
        pubTime=now if audit_status is MomentAuditStatusEnum.NORMAL else None,
        created_at=now,
        updated_at=now,
    )
    session.add(dyn)
    await session.flush()
    # 2.36.0：通用 Feed 元数据行（comprehensive_feed 推荐流候选来自本表，
    # normal + pubTime 才会入候选；auditing 时不写 pubTime）
    session.add(
        TResourceFeed(
            bizType=InteractionBizTypeEnum.DYNAMIC,
            bizId=did,
            mid=mid,
            auditStatus="normal",
            pubTime=now if audit_status is MomentAuditStatusEnum.NORMAL else None,
            tags=[topic_id] if topic_id else [],
        )
    )
    await session.commit()
    return did


async def _cleanup(topics: list[int], moment_ids: list[int]) -> None:
    async with new_session() as s:
        for did in moment_ids:
            await s.exec(text(f"DELETE FROM TResourceFeed WHERE bizType = 1 AND bizId = {did}"))
            await s.exec(text(f"DELETE FROM TMoment WHERE dynId = {did}"))
        for tid in topics:
            await s.exec(text(f"DELETE FROM TMomentTopic WHERE topicId = {tid}"))
        await s.commit()


# ==================== 话题广场（P5-T1）====================


async def test_topic_square_ordering_and_paging():
    real_topics = await fetch_real_topics(4)
    name_a = real_topics[0].topic_name if real_topics else "a"
    name_b = real_topics[1].topic_name if len(real_topics) > 1 else "b"
    async with new_session() as s:
        await _seed_topic(s, _new_topic(T_TOPIC_A, name=name_a, is_hot=0, sort_weight=1, dyn_count=5))
        await _seed_topic(s, _new_topic(T_TOPIC_B, name=name_b, is_hot=1, sort_weight=0, dyn_count=3))

        # 默认广场：热门优先（isHot desc）→ sortWeight → dynCount
        resp = await MomentTopicService.topic_square(s, page=1, page_size=20)
        assert isinstance(resp, MomentTopicSquareResp)
        names = [t.topicName for t in resp.items]
        assert names[0] == name_b  # isHot=1 优先
        assert names[1] == name_a

        # 仅热门
        hot = await MomentTopicService.topic_square(s, page=1, page_size=20, hot_only=True)
        assert [t.topicName for t in hot.items] == [name_b]

        # 失效话题即便 dynCount 大，因 isHot=0 排在 hot 之后
        await _cleanup([T_TOPIC_A, T_TOPIC_B], [])
        # 分页：page_size=1 时第二页命中 hasMore
        name_c = real_topics[2].topic_name if len(real_topics) > 2 else "c"
        name_d = real_topics[3].topic_name if len(real_topics) > 3 else "d"
        await _seed_topic(s, _new_topic(9200003, name=name_c, dyn_count=9))
        await _seed_topic(s, _new_topic(9200004, name=name_d, dyn_count=1))
        p1 = await MomentTopicService.topic_square(s, page=1, page_size=1)
        assert len(p1.items) == 1
        assert p1.hasMore is True
        p2 = await MomentTopicService.topic_square(s, page=2, page_size=1)
        assert len(p2.items) == 1
        assert p2.hasMore is False
        await _cleanup([9200003, 9200004], [])


# ==================== 话题 Feed（P5-T2）====================


async def test_topic_feed_filters_normal_and_topic():
    real_topics = await fetch_real_topics(1)
    topic_name = real_topics[0].topic_name if real_topics else "a"
    async with new_session() as s:
        await _seed_topic(s, _new_topic(T_TOPIC_A, name=topic_name))
        # 同话题 normal
        m1 = await _seed_moment(s, T_MID, topic_id=T_TOPIC_A)
        # 同话题 auditing（不可见）
        m2 = await _seed_moment(s, T_MID, topic_id=T_TOPIC_A, audit_status=MomentAuditStatusEnum.AUDITING)
        # 其他话题 normal（不应出现）
        m3 = await _seed_moment(s, T_MID, topic_id=T_TOPIC_B)

        resp = await MomentFeedService.topic_feed(s, topic_id=T_TOPIC_A, viewer_mid=T_MID)
        ids = {it.dynId for it in resp.items}
        assert m1 in ids
        assert m2 not in ids
        assert m3 not in ids
        assert resp.topicName == topic_name
        assert resp.hasMore is False

        # extend 模块（话题）应回填话题名称，供前端话题卡片显示 / 跳转话题 Feed
        ext = next(m for m in resp.items[0].modules if m.moduleType == "extend")
        assert ext.topicId == T_TOPIC_A
        assert ext.topicName == topic_name

        # 综合 Feed / 单条详情同样回填 topicName（复用同一装配管线）
        com = await MomentFeedService.comprehensive_feed(
            s, viewer_mid=T_MID, page_size=20
        )
        com_exts = [
            m
            for it in com.items
            for m in it.modules
            if m.moduleType == "extend" and m.topicId == T_TOPIC_A
        ]
        assert com_exts and all(e.topicName == topic_name for e in com_exts)

        detail = await MomentFeedService.get_detail(s, m1, viewer_mid=T_MID)
        d_ext = next(m for m in detail.modules if m.moduleType == "extend")
        assert d_ext.topicId == T_TOPIC_A
        assert d_ext.topicName == topic_name
        await _cleanup([T_TOPIC_A, T_TOPIC_B], [m1, m2, m3])


# ==================== @用户推荐（P5-T3）====================


async def test_at_recommend_empty_when_no_follows():
    async with new_session() as s:
        resp = await MomentTopicService.at_recommend(s, T_MID, page_size=20)
        assert isinstance(resp, MomentAtListResp)
        assert resp.following == []
        assert resp.followers == []


# ==================== @用户搜索（P5-T4）====================


async def test_at_search_empty_keyword():
    resp = await MomentTopicService.at_search("", page_size=20)
    assert isinstance(resp, MomentAtSearchResp)
    assert resp.items == []
    assert resp.hasMore is False


# ==================== POI 附近（P5-T5）====================


async def test_poi_nearby_dedup_by_lbs_poi():
    async with new_session() as s:
        m1 = await _seed_moment(s, T_MID, lbs_poi="北京·故宫", lbs_lat=39.9, lbs_lng=116.4)
        m2 = await _seed_moment(s, T_MID2, lbs_poi="北京·故宫", lbs_lat=39.91, lbs_lng=116.41)
        m3 = await _seed_moment(s, T_MID, lbs_poi="上海·外滩", lbs_lat=31.2, lbs_lng=121.5)

        resp = await MomentTopicService.poi_nearby(s, page=1, page_size=20)
        assert isinstance(resp, MomentPoiResp)
        pois = {it.poi: it.dynCount for it in resp.items}
        assert pois.get("北京·故宫") == 2
        assert pois.get("上海·外滩") == 1
        # 去重：只返回 2 条 POI，而非 3 条 Moment
        assert len(resp.items) == 2
        await _cleanup([], [m1, m2, m3])


# ==================== POI 关键词搜索（P5-T6）====================


async def test_poi_search_keyword_match():
    async with new_session() as s:
        m1 = await _seed_moment(s, T_MID, lbs_poi="北京·故宫")
        m2 = await _seed_moment(s, T_MID2, lbs_poi="上海·外滩")

        resp = await MomentTopicService.poi_search(s, "北京", page=1, page_size=20)
        assert isinstance(resp, MomentPoiResp)
        assert [it.poi for it in resp.items] == ["北京·故宫"]
        await _cleanup([], [m1, m2])


__all__ = [
    "test_at_recommend_empty_when_no_follows",
    "test_at_search_empty_keyword",
    "test_poi_nearby_dedup_by_lbs_poi",
    "test_poi_search_keyword_match",
    "test_topic_feed_filters_normal_and_topic",
    "test_topic_square_ordering_and_paging",
]
