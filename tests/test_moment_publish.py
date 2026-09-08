"""Phase 2 — 动态发布服务单元测试（P2-T8）。

覆盖：

- 前置校验纯逻辑（不依赖 DB）：场景合法性、字数 / @ 数量上限、富文本 → 纯文本提取。
- 发布流程（依赖真实 MySQL，与现有测试同构）：WORD 创建后 auditStatus=auditing、
  FORWARD 创建校验源动态状态、编辑 normal→auditing 且源动态 repostCount -1、
  软删 normal 转发 → 源动态 repostCount -1。

测试用独立 mid / dynId 区间，避免与 Phase B/C/E 用例互相干扰。
"""

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlmodel import col, select, text
from sqlmodel.ext.asyncio.session import AsyncSession as SQLModelAsyncSession

from app.core import database as db_mod
from app.core.config import settings
from app.core.database import new_session
from app.core.sharding import generate_moment_id
from app.models.db import (
    TMoment,
    TResourceAuditLog,
    TInteractionStat,
    TResourceFeed,
)
from bili_common.models import InteractionBizTypeEnum
from app.models.enums import (
    MomentAuditLogActionEnum,
    ResourceAuditStatusEnum,
    MomentTypeEnum,
)
from app.models.schemas.moment import (
    MomentContentNode,
    MomentCreateReq,
    MomentRemoveReq,
    MomentTopReq,
)
from app.services.moment.moment_publish import (
    _AT_MAX_COUNT,
    _CONTENT_MAX_LENGTH,
    MomentPublishService,
    _count_at,
    _nodes_to_text,
    _precheck_content,
    _precheck_scene,
)
from seed_biliopus import fetch_real_dyns


@pytest.fixture(autouse=True)
async def _bind_engine_per_test():
    """每个测试在自己的事件循环里重建 engine（与现有测试同构）。"""
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
    yield
    await engine.dispose()


# 独立 mid 区间
D_MID = 910001
D_MID2 = 910002
# 这些表测试脚本会在同一分钟里反复跑、而 moment_id 是「分钟步进雪花 + 进程重启后
# 序列从 0 重数」，短时间内多次运行会生成与上次残留行相同的主键 → 撞 TMoment.PRIMARY。
# 因此在独立测试库（BiliMessageDB_test）上跑时，每个用例跑完即清掉本模块 mid 区间的
# 动态及关联行，保证下次运行同 id 不撞已存在行（也兼容在开发库上短时间重跑）。
_PURGE_MIDS = (D_MID, D_MID2)


@pytest.fixture(autouse=True)
async def _purge_moment_rows_before():
    """每个依赖 DB 的用例跑前清理本模块 mid 区间的动态残留。

    原因：moment_id 是「分钟步进雪花 + 进程重启后序列从 0 重数」，短时间内在同一分钟
    里反复跑会生成与上次运行残留行相同的主键 → 撞 TMoment/TResourceFeed PRIMARY。
    故在跑前（setup）先清空本模块 mid 区间的动态及关联行，保证每次运行从干净状态开始。
    """
    async with new_session() as s:
        dyn_ids = {
            r[0]
            for r in (
                await s.exec(
                    text("SELECT dynId FROM TMoment WHERE mid IN (910001, 910002)")
                )
            ).all()
        }
        # TResourceFeed 可能残留（TMoment 已删时 feed 无关联动态也要清）
        dyn_ids |= {
            r[0]
            for r in (
                await s.exec(
                    text("SELECT bizId FROM TResourceFeed WHERE bizType = 1 AND mid IN (910001, 910002)")
                )
            ).all()
        }
        for did in dyn_ids:
            await s.exec(text(f"DELETE FROM TResourceAuditLog WHERE bizType = 1 AND bizId = {did}"))
            await s.exec(text(f"DELETE FROM TResourceFeed WHERE bizType = 1 AND bizId = {did}"))
            await s.exec(text(f"DELETE FROM TInteractionStat WHERE bizType = 1 AND bizId = {did}"))
            await s.exec(text(f"DELETE FROM TMoment WHERE dynId = {did}"))
        await s.commit()
    yield


def _words(text: str) -> MomentContentNode:
    return MomentContentNode(type="WORDS", text=text)


def _at(mid: int, name: str) -> MomentContentNode:
    return MomentContentNode(type="AT", bizId=str(mid), name=name)


# ==================== 纯逻辑（无 DB）====================


def test_precheck_scene_allowed():
    assert _precheck_scene("WORD") is MomentTypeEnum.WORD
    assert _precheck_scene("FORWARD") is MomentTypeEnum.FORWARD


def test_precheck_scene_rejected():
    import pytest as _pytest

    with _pytest.raises(ValueError):
        _precheck_scene("AV")


def test_precheck_content_empty():
    with pytest.raises(ValueError):
        _precheck_content([])
    with pytest.raises(ValueError):
        _precheck_content([MomentContentNode(type="WORDS", text="")])


def test_precheck_content_too_long():
    with pytest.raises(ValueError):
        _precheck_content([_words("x" * (_CONTENT_MAX_LENGTH + 1))])


def test_precheck_content_at_limit():
    nodes = [_words("hi")] + [_at(1000 + i, f"u{i}") for i in range(_AT_MAX_COUNT + 1)]
    with pytest.raises(ValueError):
        _precheck_content(nodes)


def test_nodes_to_text_and_count_at():
    nodes = [
        _words("hello "),
        _at(123, "alice"),
        _words(" "),
        MomentContentNode(type="TOPIC", bizId="9", name="news"),
        MomentContentNode(type="LINK", text="pic", jumpUrl="https://x/y.jpg"),
    ]
    assert _nodes_to_text(nodes) == "hello @alice #news#pic"
    assert _count_at(nodes) == 1


def test_create_check_allowed_scenes():
    import asyncio

    out = asyncio.get_event_loop().run_until_complete(
        MomentPublishService.create_check(D_MID, "WORD")
    )
    assert set(out["allowedScenes"]) == {"WORD", "FORWARD"}


def test_create_check_rejected_scene():
    import asyncio

    with pytest.raises(ValueError):
        asyncio.get_event_loop().run_until_complete(
            MomentPublishService.create_check(D_MID, "AV")
        )


# ==================== 依赖 DB 的发布流程 ====================


async def _cleanup(moment_ids: list[int]) -> None:
    async with new_session() as s:
        for did in moment_ids:
            await s.exec(text(f"DELETE FROM TResourceAuditLog WHERE bizType = 1 AND bizId = {did}"))
            await s.exec(
                text(f"DELETE FROM TResourceFeed WHERE bizType = 1 AND bizId = {did}")
            )
            await s.exec(
                text(f"DELETE FROM TInteractionStat WHERE bizType = 1 AND bizId = {did}")
            )
            await s.exec(text(f"DELETE FROM TMoment WHERE dynId = {did}"))
        await s.commit()


async def _seed_dynamic(
    session,
    mid: int,
    dyn_type: MomentTypeEnum,
    *,
    audit_status: ResourceAuditStatusEnum = ResourceAuditStatusEnum.NORMAL,
    repost_src: int | None = None,
) -> int:
    did = await generate_moment_id()
    now = __import__("datetime").datetime.now()
    reals = await fetch_real_dyns(1)
    content_text = reals[0].content_text if reals else "seed"
    dyn = TMoment(
        dynId=did,
        mid=mid,
        dynType=dyn_type,
        contentText=content_text,
        contentJson=[{"type": "WORDS", "text": content_text}],
        repostSrcDynId=repost_src,
        auditStatus=audit_status,
        pubTime=now if audit_status is ResourceAuditStatusEnum.NORMAL else None,
        created_at=now,
        updated_at=now,
    )
    session.add(dyn)
    await session.flush()
    # 2.36.0：计数统一 TInteractionStat + Feed 元数据 TResourceFeed
    session.add(TInteractionStat(bizType=InteractionBizTypeEnum.DYNAMIC, bizId=did))
    session.add(
        TResourceFeed(
            bizType=InteractionBizTypeEnum.DYNAMIC,
            bizId=did,
            mid=mid,
            pubTime=(now if audit_status is ResourceAuditStatusEnum.NORMAL else None),
            auditStatus=audit_status.name.lower(),
            tags=[],
        )
    )
    await session.commit()
    return did


async def test_create_word_auditing():
    """普通内容（未命中敏感词）→ 自动审核 PASS，直接 normal 对外可见（接入 B）。"""
    req = MomentCreateReq(
        scene="WORD",
        content=[_words("今天天气真好"), _at(12345, "bob")],
    )
    async with new_session() as s:
        data = await MomentPublishService.create(s, D_MID, req)
        assert data["auditStatus"] == ResourceAuditStatusEnum.NORMAL.name
        assert data["dynType"] == "WORD"
        # 2.36.0：Feed 元数据行已建（计数表惰性）
        feed = (
            await s.exec(
                select(TResourceFeed).where(
                    col(TResourceFeed.bizType) == InteractionBizTypeEnum.DYNAMIC,
                    col(TResourceFeed.bizId) == data["dynId"],
                )
            )
        ).one()
        assert feed is not None
        assert feed.auditStatus is ResourceAuditStatusEnum.NORMAL
        # 审核日志已写
        log = (
            await s.exec(
                select(TResourceAuditLog).where(
                    TResourceAuditLog.bizType == InteractionBizTypeEnum.DYNAMIC,
                    TResourceAuditLog.bizId == data["dynId"],
                )
            )
        ).first()
        assert log is not None and log.actionType is MomentAuditLogActionEnum.CREATE
        await _cleanup([data["dynId"]])


async def test_create_forward_requires_normal_src():
    async with new_session() as s:
        # 源动态是 auditing（未通过）→ 创建转发应拒绝
        src = await _seed_dynamic(
            s, D_MID2, MomentTypeEnum.WORD, audit_status=ResourceAuditStatusEnum.AUDITING
        )
        req = MomentCreateReq(
            scene="FORWARD",
            content=[_words("转发一下")],
            repostSrc={"dynId": src},
        )
        with pytest.raises(ValueError):
            await MomentPublishService.create(s, D_MID, req)
        await _cleanup([src])


async def test_create_forward_ok_and_no_repost_incr():
    async with new_session() as s:
        src = await _seed_dynamic(s, D_MID2, MomentTypeEnum.WORD)
        src_stat = (
            await s.exec(
                select(TInteractionStat).where(
                    col(TInteractionStat.bizType) == InteractionBizTypeEnum.DYNAMIC,
                    col(TInteractionStat.bizId) == src,
                )
            )
        ).one()
        before = src_stat.repostCount
        req = MomentCreateReq(
            scene="FORWARD",
            content=[_words("转发一下")],
            repostSrc={"dynId": src},
        )
        data = await MomentPublishService.create(s, D_MID, req)
        # 普通转发内容（未命中敏感词）→ 自动审核 PASS，直接 normal 对外可见（接入 B）
        assert data["auditStatus"] == ResourceAuditStatusEnum.NORMAL.name
        # 创建时源动态 repostCount 不 +1（状态机触发点⑥，仍由「通过」链路触发）
        src_stat2 = (
            await s.exec(
                select(TInteractionStat).where(
                    col(TInteractionStat.bizType) == InteractionBizTypeEnum.DYNAMIC,
                    col(TInteractionStat.bizId) == src,
                )
            )
        ).one()
        assert src_stat2.repostCount == before
        await _cleanup([src, data["dynId"]])


async def test_remove_normal_forward_decrs_src():
    async with new_session() as s:
        src = await _seed_dynamic(s, D_MID2, MomentTypeEnum.WORD)
        fwd = await _seed_dynamic(
            s, D_MID, MomentTypeEnum.FORWARD, repost_src=src
        )
        await s.exec(
            text(f"UPDATE TInteractionStat SET repostCount = 1 WHERE bizType = 1 AND bizId = {src}")
        )
        await s.commit()

        data = await MomentPublishService.remove(s, D_MID, MomentRemoveReq(dynId=fwd))
        assert data["success"] is True
        # 源动态 repostCount -1（触发点④）
        src_stat = (
            await s.exec(
                select(TInteractionStat).where(
                    col(TInteractionStat.bizType) == InteractionBizTypeEnum.DYNAMIC,
                    col(TInteractionStat.bizId) == src,
                )
            )
        ).one()
        assert src_stat.repostCount == 0
        # 软删标记
        fwd_row = (
            await s.exec(select(TMoment).where(TMoment.dynId == fwd))
        ).one()
        assert fwd_row.deletedAt is not None
        await _cleanup([src, fwd])


async def test_top_requires_normal():
    async with new_session() as s:
        dyn = await _seed_dynamic(
            s, D_MID, MomentTypeEnum.WORD, audit_status=ResourceAuditStatusEnum.AUDITING
        )
        with pytest.raises(ValueError):
            await MomentPublishService.top(s, D_MID, MomentTopReq(dynId=dyn), untop=False)
        await _cleanup([dyn])


__all__ = [
    "test_create_check_allowed_scenes",
    "test_create_check_rejected_scene",
    "test_create_forward_ok_and_no_repost_incr",
    "test_create_forward_requires_normal_src",
    "test_create_word_auditing",
    "test_nodes_to_text_and_count_at",
    "test_precheck_content_at_limit",
    "test_precheck_content_empty",
    "test_precheck_content_too_long",
    "test_precheck_scene_allowed",
    "test_precheck_scene_rejected",
    "test_remove_normal_forward_decrs_src",
    "test_top_requires_normal",
]
