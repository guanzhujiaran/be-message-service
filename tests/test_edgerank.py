"""Phase 16 — EdgeRank 推荐排序单元测试（P16-T6，2.27.0）。

覆盖：
- 公式纯函数：decay 单调递减 / 半衰期行为；权重相对大小影响排序；零互动退化；
  ``edgerank_enabled=False`` 降级为时间倒序等价分。
- 综合 Feed ``recommend``：候选集内按 EdgeRank 排序、offset 分页；``time`` 保持 pubTime 倒序。
- 话题 Feed ``hot``：排序键由「like+comment+repost 求和」替换为话题 EdgeRank（评论权重更高）。
- 话题广场：EdgeRank 排序（isHot/dynCount 加权 + 时间衰减）+ hot_only 过滤。

复用真实 MySQL 主库，独立 mid / topicId / dynId 区间避免与既有用例冲突。
recommend 模式候选窗口（``edgerank_candidate_window_hours``）在测试中收窄到 6 分钟，
seed 数据相对当前时间秒级偏移，保证候选集只含本测试种子。
"""

import datetime

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlmodel import text
from sqlmodel.ext.asyncio.session import AsyncSession as SQLModelAsyncSession

from app.core import database as db_mod
from app.core.config import settings
from app.core.database import new_session
from app.core.sharding import generate_moment_id
from app.models.db import (
    TMoment,
    TResourceDislike,
    TResourceLike,
    TResourceReport,
    TMomentTopic,
    TInteractionStat,
    TResourceFeed,
)
from bili_common.models import InteractionBizTypeEnum
from app.models.enums import (
    MomentAuditStatusEnum,
    MomentTopicAuditStatusEnum,
    MomentTypeEnum,
)
from app.services.moment.edgerank import (
    FEED_PROFILE,
    TOPIC_SQUARE_PROFILE,
    EdgeRankExtra,
    build_anon_profile,
    compute_moment_score,
    compute_topic_score,
    decay,
)
from app.services.moment.feed_engine import (
    FeedCandidate,
    PersonalSignals,
    apply_dislike_penalty,
    build_extra_from_stat,
)
from app.services.user.follow import FollowService
from app.services.moment.moment_feed import MomentFeedService
from app.services.moment.topic_feed import TopicFeedService
from app.services.moment.moment_topic import MomentTopicService

# 独立区间，避免与其它模块用例冲突
E_MID = 920101
E_MID2 = 920102
E_TOPIC_A = 9200101
E_TOPIC_B = 9200102

# 相对当前时间（recommend 候选窗口收窄后只覆盖本测试种子）
# 时间基准 datetime.now()（本地 CST）——与业务写入/灌数数据一致，避免 UTC 字面值
# 比 CST 小 8h 导致 seed 动态在库里相对灌数数据看起来更旧而被挤出候选集
_BASE = datetime.datetime.now()  # noqa: DTZ005

_MOMENT_WEIGHT_FIELDS = ("like", "comment", "repost", "view", "favorite", "share")


def _stat(
    *,
    like: int = 0,
    comment: int = 0,
    repost: int = 0,
    view: int = 0,
    favorite: int = 0,
    share: int = 0,
    coin: int = 0,
    dislike: int = 0,
) -> TInteractionStat:
    return TInteractionStat(
        bizType=InteractionBizTypeEnum.DYNAMIC,
        bizId=0,
        likeCount=like,
        commentCount=comment,
        repostCount=repost,
        viewCount=view,
        favoriteCount=favorite,
        shareCount=share,
        coinCount=coin,
        dislikeCount=dislike,
    )


def _topic(
    *,
    dyn_count: int = 0,
    view_count: int = 0,
    is_hot: int = 0,
    sort_weight: int = 0,
    pub_time: datetime.datetime | None = None,
) -> TMomentTopic:
    return TMomentTopic(
        topicId=1,
        topicName="t",
        dynCount=dyn_count,
        viewCount=view_count,
        isHot=is_hot,
        sortWeight=sort_weight,
        pubTime=pub_time,
    )


@pytest.fixture(autouse=True)
async def _bind_engine_per_test():
    """每个测试在自己的事件循环里重建 engine，并清理本模块专属区间的残留数据。

    pptr engine 也重建并绑定当前事件循环（Feed 装配回查 pptr 用户信息时，
    模块级单例 engine 绑到首个 loop 会在后续测试跨 loop 复用报
    "attached to a different loop"，与 test_moment_feed.py 处理一致）。
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
    async def _cleanup() -> None:
        async with new_session() as s:
            # 2.36.0：计数/Feed 元数据统一表（先于 TMoment 清理，bizId 依赖 dynId）
            await s.exec(
                text(
                    f"DELETE FROM TResourceFeed WHERE bizType=1 AND bizId IN "
                    f"(SELECT dynId FROM TMoment WHERE mid IN ({E_MID}, {E_MID2}))"
                )
            )
            await s.exec(
                text(
                    f"DELETE FROM TInteractionStat WHERE bizType=1 AND bizId IN "
                    f"(SELECT dynId FROM TMoment WHERE mid IN ({E_MID}, {E_MID2}))"
                )
            )
            await s.exec(
                text(
                    f"DELETE FROM TResourceFeed WHERE bizType = 1 AND bizId IN "
                    f"(SELECT dynId FROM TMoment WHERE mid IN ({E_MID}, {E_MID2}))"
                )
            )
            await s.exec(text(f"DELETE FROM TMoment WHERE mid IN ({E_MID}, {E_MID2})"))
            await s.exec(
                text(
                    f"DELETE FROM TMomentTopic WHERE topicId IN ({E_TOPIC_A}, {E_TOPIC_B})"
                )
            )
            await s.commit()

    await _cleanup()  # 预清理：确保测试开始前本模块区间干净
    yield
    await _cleanup()  # teardown 清理：避免 seed 数据残留干扰其它测试文件
    await engine.dispose()
    await pptr_engine.dispose()


# ==================== 纯函数（不依赖 DB）====================


def test_decay_monotonic():
    """decay 单调递减：age=0 → 1.0；age=half_life → 0.5；非法入参不崩。"""
    hl = 86400.0
    assert decay(0, hl) == 1.0
    assert decay(hl, hl) == pytest.approx(0.5)
    assert decay(hl * 2, hl) == pytest.approx(0.25)
    assert decay(0, 0) == 1.0
    assert decay(-1, hl) == 1.0


def test_compute_moment_score_weight_and_decay():
    """权重与时间衰减共同决定排序：同时间高互动 > 低互动；同互动新 > 旧。"""
    now = datetime.datetime(2026, 8, 21, 12, 0, 0)  # noqa: DTZ001
    hot = _stat(like=100, comment=50, repost=10, view=999, favorite=5)
    cold = _stat(like=2, view=3)
    assert compute_moment_score(hot, now, FEED_PROFILE, now=now) > compute_moment_score(
        cold, now, FEED_PROFILE, now=now
    )
    # 同互动，1 小时前 vs 1 天前：新的分数更高
    s_new = compute_moment_score(
        hot, now - datetime.timedelta(hours=1), FEED_PROFILE, now=now
    )
    s_old = compute_moment_score(
        hot, now - datetime.timedelta(hours=24), FEED_PROFILE, now=now
    )
    assert s_new > s_old


def test_compute_moment_score_zero_interaction():
    """零互动退化为最小基数 × decay，低于任意互动内容。"""
    now = datetime.datetime(2026, 8, 21, 12, 0, 0)  # noqa: DTZ001
    s_zero = compute_moment_score(_stat(), now, FEED_PROFILE, now=now)
    s_one = compute_moment_score(_stat(like=1), now, FEED_PROFILE, now=now)
    assert s_zero < s_one
    assert s_zero >= 0


def test_compute_moment_score_disabled_fallback():
    """edgerank_enabled=False：返回时间倒序等价分（时间主导，与互动无关）。"""
    now = datetime.datetime(2026, 8, 21, 12, 0, 0)  # noqa: DTZ001
    s_new = compute_moment_score(
        _stat(like=1), now, FEED_PROFILE, now=now, enabled=False
    )
    s_old = compute_moment_score(
        _stat(like=99999),
        now - datetime.timedelta(days=1),
        FEED_PROFILE,
        now=now,
        enabled=False,
    )
    assert s_new > s_old


def test_compute_topic_score_log_and_hot():
    """话题分：log 压缩 + isHot 加权 + 时间衰减。"""
    now = datetime.datetime(2026, 8, 21, 12, 0, 0)  # noqa: DTZ001

    class FakeTopic:
        dynCount = 1000
        viewCount = 500
        isHot = 1
        sortWeight = 0
        pubTime = now

    t = FakeTopic()
    s_hot = compute_topic_score(t, TOPIC_SQUARE_PROFILE, now=now)
    assert s_hot > 0
    # 同一话题 isHot 置 0 → 分数显著降低（isHot 权重 5.0 直接加权）
    t.isHot = 0
    assert compute_topic_score(t, TOPIC_SQUARE_PROFILE, now=now) < s_hot


def test_build_anon_profile_deterministic():
    """匿名随机权重：同 seed 确定、不同 seed 权重不同、扰动落在 [1±ratio]。"""
    p1 = build_anon_profile("anon-a")
    p2 = build_anon_profile("anon-a")
    p3 = build_anon_profile("anon-b")
    assert p1 == p2  # 同 seed → 同权重（匿名用户刷新稳定）
    assert p1 != p3  # 不同 seed → 权重不同（不同用户不同 feed）
    ratio = settings.edgerank_anon_perturb_ratio
    for k in _MOMENT_WEIGHT_FIELDS:
        base = float(getattr(FEED_PROFILE, k))
        v = float(getattr(p1, k))
        assert base * (1 - ratio) <= v <= base * (1 + ratio)
    # 半衰期与 FEED_PROFILE 一致（只扰动权重，不动时间衰减）
    assert p1.half_life_seconds == FEED_PROFILE.half_life_seconds


def test_compute_topic_score_disabled_fallback():
    """话题降级：返回时间等价分（新话题分更大）。"""
    now = datetime.datetime(2026, 8, 21, 12, 0, 0)  # noqa: DTZ001

    class FakeTopic:
        def __init__(self, pub_time):
            self.dynCount = 1
            self.viewCount = 0
            self.isHot = 0
            self.sortWeight = 0
            self.pubTime = pub_time

    s_new = compute_topic_score(
        FakeTopic(now), TOPIC_SQUARE_PROFILE, now=now, enabled=False
    )
    s_old = compute_topic_score(
        FakeTopic(now - datetime.timedelta(days=1)),
        TOPIC_SQUARE_PROFILE,
        now=now,
        enabled=False,
    )
    assert s_new > s_old


# ==================== DB 行为（依赖真实 MySQL）====================


async def _seed_moment(
    session,
    mid: int,
    *,
    seconds_ago: int = 0,
    like: int = 0,
    comment: int = 0,
    repost: int = 0,
    view: int = 0,
    favorite: int = 0,
) -> int:
    """seed 一条 normal 动态 + 指定计数快照（相对当前时间秒级偏移）。"""
    did = await generate_moment_id()
    now = _BASE - datetime.timedelta(seconds=seconds_ago)
    dyn = TMoment(
        dynId=did,
        mid=mid,
        dynType=MomentTypeEnum.WORD,
        contentText="edgerank seed",
        contentJson=[{"type": "WORDS", "text": "edgerank seed"}],
        auditStatus=MomentAuditStatusEnum.NORMAL,
        pubTime=now,
        created_at=now,
        updated_at=now,
    )
    session.add(dyn)
    # 2.36.0：计数统一 TInteractionStat(dynamic 行) + 通用 Feed 元数据 TResourceFeed
    session.add(
        TInteractionStat(
            bizType=InteractionBizTypeEnum.DYNAMIC,
            bizId=did,
            likeCount=like,
            commentCount=comment,
            repostCount=repost,
            viewCount=view,
            favoriteCount=favorite,
        )
    )
    session.add(
        TResourceFeed(
            bizType=InteractionBizTypeEnum.DYNAMIC,
            bizId=did,
            mid=mid,
            pubTime=now,
            auditStatus="normal",
            tags=[],
        )
    )
    return did


def _seed_topic(
    session,
    topic_id: int,
    *,
    name: str,
    is_hot: int = 0,
    sort_weight: int = 0,
    dyn_count: int = 0,
    view_count: int = 0,
    seconds_ago: int = 0,
) -> None:
    now = _BASE - datetime.timedelta(seconds=seconds_ago)
    session.add(
        TMomentTopic(
            topicId=topic_id,
            topicName=name,
            isHot=is_hot,
            sortWeight=sort_weight,
            dynCount=dyn_count,
            viewCount=view_count,
            auditStatus=MomentTopicAuditStatusEnum.NORMAL,
            pubTime=now,
            creatorMid=0,
            created_at=now,
            updated_at=now,
        )
    )


async def test_comprehensive_feed_recommend_sorts_by_edgerank(monkeypatch):
    """综合 Feed recommend：高互动排前（覆盖更新但低互动的动态），showlist 去重。"""
    monkeypatch.setattr(settings, "edgerank_candidate_window_hours", 0.1)
    async with new_session() as s:
        a = await _seed_moment(s, E_MID, seconds_ago=60, like=100, comment=50)  # 高互动
        b = await _seed_moment(s, E_MID, seconds_ago=60, like=10, comment=2)  # 中互动
        c = await _seed_moment(s, E_MID, seconds_ago=30, like=1)  # 最新但低互动
        await s.commit()

        resp = await MomentFeedService.comprehensive_feed(
            s, page_size=2, sort="recommend", viewer_mid=None
        )
        assert [it.dynId for it in resp.items] == [a, b]
        assert resp.hasMore is True

        # 2.32.0：排除已展示项（last_showlist）后继续取后续推荐
        resp2 = await MomentFeedService.comprehensive_feed(
            s, page_size=2, sort="recommend", viewer_mid=None, last_showlist=[a, b]
        )
        assert [it.dynId for it in resp2.items] == [c]
        assert resp2.hasMore is False


async def test_comprehensive_feed_time_keeps_pubtime_order(monkeypatch):
    """综合 Feed time：保持 pubTime 倒序（回归，不因 sort 参数受影响）。

    time 模式查询全表（不限 mid），库内可能有其它模块残留动态，故只断言
    本测试 seed 的三条动态相对顺序。
    """
    monkeypatch.setattr(settings, "edgerank_candidate_window_hours", 0.1)
    async with new_session() as s:
        a = await _seed_moment(s, E_MID, seconds_ago=120, like=0)
        b = await _seed_moment(s, E_MID, seconds_ago=60, like=0)
        c = await _seed_moment(s, E_MID, seconds_ago=30, like=0)
        await s.commit()

        resp = await MomentFeedService.comprehensive_feed(
            s, page=1, page_size=50, sort="time", viewer_mid=None
        )
        mine = [it.dynId for it in resp.items if it.dynId in {a, b, c}]
        assert mine == [c, b, a]


async def test_comprehensive_feed_recommend_showlist_dedup(monkeypatch):
    """综合 Feed recommend：last_showlist 去重——排除已展示项后继续取后续推荐。

    推荐流无 page/offset 语义：不带 showlist 返回排序前 N 条；
    带已展示列表后返回未展示的后续项，直到候选耗尽 hasMore=False。
    """
    monkeypatch.setattr(settings, "edgerank_candidate_window_hours", 0.1)
    async with new_session() as s:
        ids = [
            await _seed_moment(s, E_MID, seconds_ago=60, like=10 - i)
            for i in range(5)  # like: 10,9,8,7,6 → EdgeRank 序即 seed 序
        ]
        await s.commit()

        page1 = await MomentFeedService.comprehensive_feed(
            s, page_size=2, sort="recommend", viewer_mid=None
        )
        assert [it.dynId for it in page1.items] == ids[:2]

        # 已展示 ids[:2]：排除后取后续
        page2 = await MomentFeedService.comprehensive_feed(
            s,
            page_size=2,
            sort="recommend",
            viewer_mid=None,
            last_showlist=ids[:2],
        )
        assert [it.dynId for it in page2.items] == ids[2:4]

        # 已展示 ids[:4]：只剩最后一条，hasMore=False
        page3 = await MomentFeedService.comprehensive_feed(
            s,
            page_size=2,
            sort="recommend",
            viewer_mid=None,
            last_showlist=ids[:4],
        )
        assert [it.dynId for it in page3.items] == ids[4:]
        assert page3.hasMore is False
        # 推荐流无游标语义：updateBaseline/historyOffset 置空
        assert page3.updateBaseline is None
        assert page3.historyOffset is None


async def test_comprehensive_feed_recommend_personalized_follow(monkeypatch):
    """个性化（2.33.0）：关注作者的低互动动态排在未关注的高互动之前；未登录退化为全局。

    构造：``hot``（未关注，like=2，base≈2.0）vs ``cold``（被关注，零互动，
    base≈0.1 + follow 权重 3.0 = 3.1）。登录用户关注 E_MID2 后 cold 应反超 hot。
    """
    monkeypatch.setattr(settings, "edgerank_candidate_window_hours", 0.1)
    viewer = 990101
    async with new_session() as s:
        hot = await _seed_moment(s, E_MID, seconds_ago=60, like=2)
        cold = await _seed_moment(s, E_MID2, seconds_ago=60, like=0)
        await FollowService.follow(s, viewer, E_MID2)
        await s.commit()
    try:
        async with new_session() as s:
            # 未登录：纯全局排序 → hot（互动高）在前
            anon = await MomentFeedService.comprehensive_feed(
                s, page_size=2, sort="recommend", viewer_mid=None
            )
            assert [it.dynId for it in anon.items][:2] == [hot, cold]
            # 登录且关注 E_MID2：cold 获得 follow 加分 → 反超 hot
            me = await MomentFeedService.comprehensive_feed(
                s, page_size=2, sort="recommend", viewer_mid=viewer
            )
            assert [it.dynId for it in me.items][0] == cold
    finally:
        async with new_session() as s:
            await s.exec(text(f"DELETE FROM msg_user_follow WHERE mid = {viewer}"))
            await s.commit()


async def test_comprehensive_feed_anon_randomized_stable(monkeypatch):
    """未登录匿名随机：同 uniq_id 两次请求排序稳定（不因随机每次乱跳）。"""
    monkeypatch.setattr(settings, "edgerank_candidate_window_hours", 0.1)
    async with new_session() as s:
        a = await _seed_moment(s, E_MID, seconds_ago=60, like=1)
        b = await _seed_moment(s, E_MID2, seconds_ago=60, like=0)
        await s.commit()
    async with new_session() as s:
        r1 = await MomentFeedService.comprehensive_feed(
            s, page_size=2, sort="recommend", viewer_mid=None, uniq_id="anon-x"
        )
        r2 = await MomentFeedService.comprehensive_feed(
            s, page_size=2, sort="recommend", viewer_mid=None, uniq_id="anon-x"
        )
        assert [it.dynId for it in r1.items] == [it.dynId for it in r2.items]


async def test_comprehensive_feed_anon_randomize_disabled(monkeypatch):
    """未登录 + 匿名随机开关关闭：走全局 FEED_PROFILE（like 高在前）。"""
    monkeypatch.setattr(settings, "edgerank_candidate_window_hours", 0.1)
    monkeypatch.setattr(settings, "edgerank_anon_randomize_enabled", False)
    async with new_session() as s:
        a = await _seed_moment(s, E_MID, seconds_ago=60, like=2)
        b = await _seed_moment(s, E_MID2, seconds_ago=60, like=0)
        await s.commit()
    async with new_session() as s:
        r = await MomentFeedService.comprehensive_feed(
            s, page_size=2, sort="recommend", viewer_mid=None, uniq_id="anon-y"
        )
        assert [it.dynId for it in r.items][0] == a


async def test_comprehensive_feed_report_penalty(monkeypatch):
    """2.37.0：pending 举报降权（resourceType+bizId 统计）——同互动被举报者排后。

    匿名（viewer_mid=None）会触发随机权重扰动，可能盖过小额举报降权导致顺序不稳定，
    故本测试关闭匿名随机化，使排序退化为确定性全局 EdgeRank（举报降权可复现）。
    """
    monkeypatch.setattr(settings, "edgerank_candidate_window_hours", 0.1)
    monkeypatch.setattr(settings, "edgerank_anon_randomize_enabled", False)
    async with new_session() as s:
        a = await _seed_moment(s, E_MID, seconds_ago=60, like=2)
        b = await _seed_moment(s, E_MID2, seconds_ago=60, like=2)
        # a 被举报（pending，bizType=dynamic）。TResourceReport 继承 ReportBase，
        # bizType/auditStatus 均为 INTEGER（ReportAuditStatusEnum.PENDING=1）
        s.add(
            TResourceReport(
                bizType=int(InteractionBizTypeEnum.DYNAMIC),
                bizId=a,
                accusedMid=E_MID,
                reportMid=E_MID2,
                reasonType=1,
                auditStatus=1,  # ReportAuditStatusEnum.PENDING
            )
        )
        await s.commit()
    try:
        async with new_session() as s:
            resp = await MomentFeedService.comprehensive_feed(
                s, page_size=2, sort="recommend", viewer_mid=None
            )
            # 同互动（like=2）+ 同时间：基础分相同；a 有 pending 举报 → 降权排后
            assert [it.dynId for it in resp.items] == [b, a]
    finally:
        async with new_session() as s:
            await s.exec(text(f"DELETE FROM TResourceReport WHERE bizId IN ({a}, {b})"))
            await s.commit()


def test_apply_dislike_penalty_hits_only_disliked(monkeypatch):
    """2.62.0：点踩个人化降权纯函数——命中点踩者大幅降权，未命中 / 开关关闭原分返回。"""
    monkeypatch.setattr(settings, "edgerank_personal_dislike_enabled", True)
    monkeypatch.setattr(settings, "edgerank_personal_dislike_scale", 0.3)
    monkeypatch.setattr(settings, "edgerank_personal_dislike_weight", 5.0)
    disliked_cand = FeedCandidate(biz_type="dynamic", biz_id=1, mid=1)
    normal_cand = FeedCandidate(biz_type="dynamic", biz_id=2, mid=2)
    signals = PersonalSignals(disliked={1})
    # 未命中点踩集合 → 原分返回
    assert apply_dislike_penalty(10.0, normal_cand, signals) == 10.0
    # 命中 → score * scale - weight（高分被显著压低，低分直接转负沉底）
    assert apply_dislike_penalty(10.0, disliked_cand, signals) == pytest.approx(
        10.0 * 0.3 - 5.0
    )
    assert apply_dislike_penalty(1.0, disliked_cand, signals) < 0
    # 开关关闭 → 退化为原分
    monkeypatch.setattr(settings, "edgerank_personal_dislike_enabled", False)
    assert apply_dislike_penalty(10.0, disliked_cand, signals) == 10.0


async def test_comprehensive_feed_personal_dislike_penalty(monkeypatch):
    """2.62.0：点踩 → 对点踩者本人大幅降权；不影响其他人的排序。

    构造：a / b 同互动（like=2）、同时间（基础分相同）；viewer 点踩 a。
    关闭匿名随机排除扰动，保证排序确定。
    """
    monkeypatch.setattr(settings, "edgerank_candidate_window_hours", 0.1)
    monkeypatch.setattr(settings, "edgerank_anon_randomize_enabled", False)
    viewer = 990201
    other = 990202
    async with new_session() as s:
        a = await _seed_moment(s, E_MID, seconds_ago=60, like=2)
        b = await _seed_moment(s, E_MID2, seconds_ago=60, like=2)
        s.add(
            TResourceDislike(
                bizType=InteractionBizTypeEnum.DYNAMIC,
                bizId=a,
                mid=viewer,
            )
        )
        await s.commit()
    try:
        async with new_session() as s:
            me = await MomentFeedService.comprehensive_feed(
                s, page_size=2, sort="recommend", viewer_mid=viewer
            )
            # 基础分相同 + 我点踩过 a → a 对本人降权，排到 b 之后
            assert [it.dynId for it in me.items] == [b, a]
            # 他人无点踩记录 → 点踩不越权影响别人的候选（内容仍可见，只是本人靠后）
            other_feed = await MomentFeedService.comprehensive_feed(
                s, page_size=2, sort="recommend", viewer_mid=other
            )
            assert set(it.dynId for it in other_feed.items) == {a, b}
    finally:
        async with new_session() as s:
            await s.exec(text(f"DELETE FROM TResourceDislike WHERE mid = {viewer}"))
            await s.exec(text(f"DELETE FROM TFeedImpression WHERE viewerKey = 'mid:{viewer}'"))
            await s.commit()


async def test_comprehensive_feed_personal_dislike_exclude(monkeypatch):
    """2.62.0：`edgerank_personal_dislike_exclude` 开启时，点踩过的资源直接剔除出我的 Feed。"""
    monkeypatch.setattr(settings, "edgerank_candidate_window_hours", 0.1)
    monkeypatch.setattr(settings, "edgerank_anon_randomize_enabled", False)
    monkeypatch.setattr(settings, "edgerank_personal_dislike_exclude", True)
    viewer = 990203
    async with new_session() as s:
        a = await _seed_moment(s, E_MID, seconds_ago=60, like=2)
        b = await _seed_moment(s, E_MID2, seconds_ago=60, like=2)
        s.add(
            TResourceDislike(
                bizType=InteractionBizTypeEnum.DYNAMIC,
                bizId=a,
                mid=viewer,
            )
        )
        await s.commit()
    try:
        async with new_session() as s:
            me = await MomentFeedService.comprehensive_feed(
                s, page_size=2, sort="recommend", viewer_mid=viewer
            )
            ids = [it.dynId for it in me.items]
            assert ids == [b]  # a 被剔除
    finally:
        async with new_session() as s:
            await s.exec(text(f"DELETE FROM TResourceDislike WHERE mid = {viewer}"))
            await s.exec(text(f"DELETE FROM TFeedImpression WHERE viewerKey = 'mid:{viewer}'"))
            await s.commit()


def test_compute_moment_score_author_fans_level_and_report():
    """2.37.0：作者粉丝/等级加权 + 举报数降权进入打分。"""
    now = datetime.datetime(2026, 8, 21, 12, 0, 0)  # noqa: DTZ001
    base = _stat(like=1, view=100)
    s0 = compute_moment_score(base, now, FEED_PROFILE, now=now, extra=EdgeRankExtra())
    s1 = compute_moment_score(
        base,
        now,
        FEED_PROFILE,
        now=now,
        extra=EdgeRankExtra(fans=10000, level=5),
    )
    s2 = compute_moment_score(
        base,
        now,
        FEED_PROFILE,
        now=now,
        extra=EdgeRankExtra(report_count=3),
    )
    assert s1 > s0  # 粉丝 log 加权 + 等级线性加权 → 加分
    assert s2 < s0  # 举报降权 → 扣分


async def test_comprehensive_feed_recommend_personalized_liked_author(monkeypatch):
    """个性化：点赞过的作者的新动态获得加分（liked_author 信号）。

    回归 2.34.0 取值 bug：``_load_personal_signals`` 单列 select 曾按
    ``r[0]`` 取标量导致 ``TypeError: 'int' object is not subscriptable``——
    本用例构造点赞历史（like_rows 非空）走完整路径。
    """
    monkeypatch.setattr(settings, "edgerank_candidate_window_hours", 0.1)
    viewer = 990103
    async with new_session() as s:
        # 历史动态（E_MID 作者，1h 前 → 不在收窄候选窗口），viewer 点赞过
        hist = await _seed_moment(s, E_MID, seconds_ago=3600, like=0)
        s.add(
            TResourceLike(
                mid=viewer,
                bizType=InteractionBizTypeEnum.DYNAMIC,
                bizId=hist,
                dynId=hist,
            )
        )
        # 候选：a 作者被点赞过（+1.5）、b 作者未互动（零互动 base 0.1）
        a = await _seed_moment(s, E_MID, seconds_ago=60, like=0)
        b = await _seed_moment(s, E_MID2, seconds_ago=60, like=0)
        await s.commit()
    try:
        async with new_session() as s:
            me = await MomentFeedService.comprehensive_feed(
                s, page_size=2, sort="recommend", viewer_mid=viewer
            )
            assert [it.dynId for it in me.items][0] == a
    finally:
        async with new_session() as s:
            await s.exec(text(f"DELETE FROM TResourceLike WHERE mid = {viewer}"))
            await s.commit()


async def test_comprehensive_feed_recommend_personalized_disabled(monkeypatch):
    """个性化开关关闭：登录用户退化为全局排序（与未登录一致）。"""
    monkeypatch.setattr(settings, "edgerank_candidate_window_hours", 0.1)
    monkeypatch.setattr(settings, "edgerank_personalized_enabled", False)
    viewer = 990102
    async with new_session() as s:
        hot = await _seed_moment(s, E_MID, seconds_ago=60, like=2)
        cold = await _seed_moment(s, E_MID2, seconds_ago=60, like=0)
        await FollowService.follow(s, viewer, E_MID2)
        await s.commit()
    try:
        async with new_session() as s:
            me = await MomentFeedService.comprehensive_feed(
                s, page_size=2, sort="recommend", viewer_mid=viewer
            )
            assert [it.dynId for it in me.items][0] == hot  # 关注加分不生效
    finally:
        async with new_session() as s:
            await s.exec(text(f"DELETE FROM msg_user_follow WHERE mid = {viewer}"))
            await s.commit()


async def test_topic_feed_hot_uses_topic_edgerank():
    """话题 Feed hot：EdgeRank（评论权重 1.8 > 点赞 1.2）替代原求和排序。

    构造：A 评论多（求和排序低）、B 点赞+评论多（求和排序高），但 EdgeRank 下
    评论权重更高的 A 应靠前。
    """
    async with new_session() as s:
        _seed_topic(s, E_TOPIC_A, name="er-topic-a", seconds_ago=120)
        a = await _seed_moment(s, E_MID, seconds_ago=60, comment=100)
        b = await _seed_moment(s, E_MID2, seconds_ago=30, like=80, comment=30)
        # autoflush=False：text UPDATE 前需先 flush 让 TMoment 行落库，否则匹配不到
        await s.flush()
        await s.exec(
            text(
                f"UPDATE TMoment SET topicId={E_TOPIC_A} WHERE dynId IN ({a}, {b})"
            )
        )
        await s.commit()

        resp = await TopicFeedService.topic_feed(
            s, topic_id=E_TOPIC_A, page=1, page_size=10, sort="hot", viewer_mid=None
        )
        dyn_ids = [it.dynId for it in resp.items]
        assert dyn_ids[0] == a, dyn_ids


async def test_topic_square_uses_topic_edgerank():
    """话题广场：EdgeRank 排序——isHot + dynCount 加权的热门话题排在更新话题之前。"""
    async with new_session() as s:
        _seed_topic(
            s, E_TOPIC_A, name="er-square-a",
            is_hot=1, dyn_count=1000, seconds_ago=120,
        )
        _seed_topic(
            s, E_TOPIC_B, name="er-square-b",
            is_hot=0, dyn_count=10, seconds_ago=30,
        )
        await s.commit()

        # 2.46.0 推荐流：无 page 参数，page_size 截断
        resp = await MomentTopicService.topic_square(s, page_size=20)
        ids = [it.topicId for it in resp.items]
        # 库内可能残留其它话题，只断言相对顺序：A（热门）在 B（新但冷）之前
        assert ids.index(E_TOPIC_A) < ids.index(E_TOPIC_B)


async def test_topic_square_hot_only_filters():
    """热搜 hot_only：仅返回 isHot=1 的话题（EdgeRank 排序下仍生效）。"""
    async with new_session() as s:
        _seed_topic(s, E_TOPIC_A, name="er-hot-a", is_hot=1, dyn_count=5, seconds_ago=30)
        _seed_topic(s, E_TOPIC_B, name="er-hot-b", is_hot=0, dyn_count=999, seconds_ago=30)
        await s.commit()

        # 2.46.0 推荐流：无 page 参数，page_size 截断
        resp = await MomentTopicService.topic_square(
            s, page_size=20, hot_only=True
        )
        ids = [it.topicId for it in resp.items]
        assert E_TOPIC_A in ids
        assert E_TOPIC_B not in ids


# ==================== 2.47.0：通用化 + 曝光去重 ====================


def test_build_extra_from_stat_uses_all_interaction_fields():
    """互动特征纯 stat 驱动：favorite/share/coin 一并计入正向互动（此前被浪费）。

    验证全量互动字段参与 engagement，且无资源类型分支（任意 TInteractionStat 复用）。
    """
    # favorite/share/coin 各 +1：应显著抬升 engagement
    s1 = _stat(like=2, favorite=100, share=50, coin=10, view=1)
    eng1, exp1, dr1 = build_extra_from_stat(s1)
    s2 = _stat(like=2, view=1)  # 仅点赞，正向互动少
    eng2, exp2, _ = build_extra_from_stat(s2)
    assert eng1 > eng2, (eng1, eng2)
    # 曝光量即 viewCount
    assert exp1 == 1.0 and exp2 == 1.0
    # 点踩占比：dislike/(dislike+like)
    d = _stat(like=1, dislike=3, view=10)
    _, _, ratio = build_extra_from_stat(d)
    assert ratio == 0.75
    # 空 stat 不崩，全 0
    e, x, r = build_extra_from_stat(None)
    assert (e, x, r) == (0.0, 0.0, 0.0)


async def test_comprehensive_feed_impression_dedup_no_repeat():
    """综合页曝光去重：同一登录用户第二次拉取不再下发已展示过的动态。

    viewer_mid 提供 → viewer_key=mid:{mid}，两次拉取间曝光落库；
    第二次返回的 dynId 不得与第一次重复（候选充足时不触发降级回填）。
    """
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(settings, "edgerank_candidate_window_hours", 0.1)
    try:
        async with new_session() as s:
            ids = [
                await _seed_moment(s, E_MID, seconds_ago=60, like=10 - i)
                for i in range(4)  # like 10,9,8,7 → EdgeRank 序即 seed 序
            ]
            await s.commit()
            # 清理该观众可能的历史曝光记录，保证用例可重复
            for did in ids:
                await s.exec(
                    text(
                        "DELETE FROM TFeedImpression WHERE "
                        f"viewerKey='mid:{E_MID2}' AND bizType=1 AND bizId={did}"
                    )
                )
            await s.commit()

            page1 = await MomentFeedService.comprehensive_feed(
                s, page_size=2, sort="recommend", viewer_mid=E_MID2
            )
            assert [it.dynId for it in page1.items] == ids[:2]

            # 第二次拉取：曝光去重后应返回下一页（ids[2:4]），不得重复 ids[:2]
            page2 = await MomentFeedService.comprehensive_feed(
                s, page_size=2, sort="recommend", viewer_mid=E_MID2
            )
            assert [it.dynId for it in page2.items] == ids[2:4]
    finally:
        monkeypatch.undo()
        async with new_session() as s:
            await s.exec(
                text(
                    f"DELETE FROM TFeedImpression WHERE viewerKey='mid:{E_MID2}'"
                )
            )
            await s.commit()
