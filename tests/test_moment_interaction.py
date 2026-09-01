"""Phase 4 — 动态互动 & 统计服务单元测试（P4-T8）。

覆盖：

- 点赞幂等（P4-T3）：首次 +1、重复点赞不叠加、取消 -1（>0 兜底）。
- 仅 normal 可点赞：auditing/rejected/已软删拒绝。
- 浏览去重（P4-T4）：首次 counted=True 且 viewCount+1；同日二次 counted=False 不累加。
- 举报（P4-T5）：写入 TResourceReport，不改变 auditStatus。
- repostCount 状态机（P4-T2）：源动态 ±1 原子操作、>0 防负数。

测试用独立 mid / dynId（D_MID 前缀）区间，每测试重建 message + pptr engine。
"""

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlmodel import col, select, text
from sqlmodel.ext.asyncio.session import AsyncSession as SQLModelAsyncSession

from app.core import database as db_mod
from app.core import database as db_mod_pptr
from app.core.config import settings
from app.core.database import new_session
from app.core.sharding import generate_moment_id
from app.models.db import (
    TMoment,
    TMomentLike,
    TResourceReport,
    TInteractionStat,
    TInteractionViewLog,
    TResourceFeed,
)
from app.models.enums import (
    InteractionBizTypeEnum,
    MomentAuditStatusEnum,
    MomentReportReasonEnum,
    MomentTypeEnum,
)
from app.models.schemas.moment import MomentReportReq
from app.services.interaction_actions import get_biz
from app.services.moment.moment_stat import MomentStatService
from seed_biliopus import RealDyn, fetch_real_dyns

D_MID = 930001
D_MID2 = 930002


async def _new_moment_id() -> int:
    return await generate_moment_id()


import datetime

_BASE = datetime.datetime(2026, 8, 1, 12, 0, 0)  # noqa: DTZ001


async def _seed(session, mid, *, real: RealDyn | None = None, **kw) -> int:
    did = await _new_moment_id()
    audit = kw.get("audit", MomentAuditStatusEnum.NORMAL)
    now = _BASE - datetime.timedelta(minutes=kw.get("minutes_ago", 0))
    content_text = real.content_text if real else "seed"
    dyn = TMoment(
        dynId=did,
        mid=mid,
        dynType=kw.get("dyn_type", MomentTypeEnum.WORD),
        contentText=content_text,
        contentJson=[{"type": "WORDS", "text": content_text}],
        repostSrcDynId=kw.get("repost_src"),
        auditStatus=audit,
        pubTime=now if audit is MomentAuditStatusEnum.NORMAL else None,
        deletedAt=(now if kw.get("deleted") else None),
        created_at=now,
        updated_at=now,
    )
    session.add(dyn)
    return did


async def _commit_seed(session, mid, **kw) -> int:
    did = await _seed(session, mid, **kw)
    await session.flush()
    audit = kw.get("audit", MomentAuditStatusEnum.NORMAL)
    pub = None
    if audit is MomentAuditStatusEnum.NORMAL:
        pub = _BASE - datetime.timedelta(minutes=kw.get("minutes_ago", 0))
    # 2.36.0：计数统一 TInteractionStat + Feed 元数据 TResourceFeed
    session.add(TInteractionStat(bizType=InteractionBizTypeEnum.DYNAMIC, bizId=did))
    session.add(
        TResourceFeed(
            bizType=InteractionBizTypeEnum.DYNAMIC,
            bizId=did,
            mid=mid,
            pubTime=pub,
            auditStatus=audit.name.lower(),
            tags=[],
        )
    )
    await session.commit()
    return did


async def _cleanup(dids: list[int]) -> None:
    async with new_session() as s:
        for d in dids:
            await s.exec(text(f"DELETE FROM TMomentLike WHERE dynId = {d}"))
            await s.exec(
                text(f"DELETE FROM TInteractionViewLog WHERE bizType = 1 AND bizId = {d}")
            )
            # TResourceReport 已统一为 ReportBase 结构（bizType+bizId 数值编码，不再有 dynId 列）
            await s.exec(
                text(
                    f"DELETE FROM TResourceReport WHERE bizType = 1 AND bizId = {d}"
                )
            )
            await s.exec(
                text(f"DELETE FROM TInteractionStat WHERE bizType = 1 AND bizId = {d}")
            )
            await s.exec(
                text(f"DELETE FROM TResourceFeed WHERE bizType = 1 AND bizId = {d}")
            )
            await s.exec(text(f"DELETE FROM TMoment WHERE dynId = {d}"))
        await s.commit()


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
        bind=engine, class_=SQLModelAsyncSession, expire_on_commit=False, autoflush=False
    )
    pptr_engine = create_async_engine(url=settings.postgres_pptr_url, pool_pre_ping=True, future=True)
    db_mod_pptr.pptr_engine = pptr_engine
    db_mod_pptr.pptr_session_maker = async_sessionmaker(
        bind=pptr_engine, class_=SQLModelAsyncSession, expire_on_commit=False, autoflush=False
    )
    yield
    await engine.dispose()
    await pptr_engine.dispose()


async def test_thumb_first_like():
    dids = []
    reals = await fetch_real_dyns(1)
    async with new_session() as s:
        dids.append(await _commit_seed(s, D_MID, real=reals[0]))
    try:
        async with new_session() as s:
            is_like, cnt = await get_biz(InteractionBizTypeEnum.DYNAMIC, s, dids[0], D_MID2).like(up=1)
            assert is_like is True and cnt == 1
            like = (await s.exec(select(TMomentLike).where(TMomentLike.dynId == dids[0]))).first()
            assert like is not None and like.mid == D_MID2
    finally:
        await _cleanup(dids)


async def test_thumb_idempotent():
    dids = []
    reals = await fetch_real_dyns(1)
    async with new_session() as s:
        dids.append(await _commit_seed(s, D_MID, real=reals[0]))
    try:
        async with new_session() as s:
            await get_biz(InteractionBizTypeEnum.DYNAMIC, s, dids[0], D_MID2).like(up=1)
            is_like, cnt = await get_biz(InteractionBizTypeEnum.DYNAMIC, s, dids[0], D_MID2).like(up=1)
            assert is_like is True and cnt == 1  # 不叠加
            n = (await s.exec(select(TMomentLike).where(TMomentLike.dynId == dids[0]))).all()
            assert len(n) == 1
    finally:
        await _cleanup(dids)


async def test_thumb_cancel():
    dids = []
    reals = await fetch_real_dyns(1)
    async with new_session() as s:
        dids.append(await _commit_seed(s, D_MID, real=reals[0]))
    try:
        async with new_session() as s:
            await get_biz(InteractionBizTypeEnum.DYNAMIC, s, dids[0], D_MID2).like(up=1)
            is_like, cnt = await get_biz(InteractionBizTypeEnum.DYNAMIC, s, dids[0], D_MID2).like(up=2)
            assert is_like is False and cnt == 0
            n = (await s.exec(select(TMomentLike).where(TMomentLike.dynId == dids[0]))).all()
            assert len(n) == 0
    finally:
        await _cleanup(dids)


async def test_thumb_reject_non_normal():
    dids = []
    reals = await fetch_real_dyns(1)
    async with new_session() as s:
        dids.append(await _commit_seed(s, D_MID, real=reals[0], audit=MomentAuditStatusEnum.AUDITING))
    try:
        async with new_session() as s:
            with pytest.raises(ValueError):
                await get_biz(InteractionBizTypeEnum.DYNAMIC, s, dids[0], D_MID2).like(up=1)
    finally:
        await _cleanup(dids)


async def test_view_dedup():
    dids = []
    reals = await fetch_real_dyns(1)
    async with new_session() as s:
        dids.append(await _commit_seed(s, D_MID, real=reals[0]))
    try:
        async with new_session() as s:
            c1 = await MomentStatService.report_view(s, dids[0], D_MID2)
            c2 = await MomentStatService.report_view(s, dids[0], D_MID2)
            assert c1 is True and c2 is False  # 同日重复访问不重复计 Stat
            await s.commit()
            stat = (
                await s.exec(
                    select(TInteractionStat).where(
                        col(TInteractionStat.bizType) == InteractionBizTypeEnum.DYNAMIC,
                        col(TInteractionStat.bizId) == dids[0],
                    )
                )
            ).one()
            assert stat.viewCount == 1  # 自然日窗口内只计一次
            vlog = (
                await s.exec(
                    select(TInteractionViewLog).where(
                        col(TInteractionViewLog.bizType) == InteractionBizTypeEnum.DYNAMIC,
                        col(TInteractionViewLog.bizId) == dids[0],
                        col(TInteractionViewLog.mid) == D_MID2,
                    )
                )
            ).one()
            assert vlog.viewCount == 2  # 单行内累加（每用户每资源一行）
    finally:
        await _cleanup(dids)


async def test_report_writes_and_keeps_status():
    dids = []
    reals = await fetch_real_dyns(1)
    async with new_session() as s:
        dids.append(await _commit_seed(s, D_MID, real=reals[0]))
    try:
        async with new_session() as s:
            await get_biz(InteractionBizTypeEnum.DYNAMIC, s, dids[0], D_MID2).report(
                reason_type=MomentReportReasonEnum.FAKE_INFO.value
            )
            # TResourceReport 已统一为 ReportBase 结构（bizType+bizId，不再有 dynId 列）
            rep = (
                await s.exec(
                    select(TResourceReport).where(TResourceReport.bizId == dids[0])
                )
            ).one()
            assert rep.reportMid == D_MID2 and rep.accusedMid == D_MID
            dyn = (await s.exec(select(TMoment).where(TMoment.dynId == dids[0]))).one()
            assert dyn.auditStatus is MomentAuditStatusEnum.NORMAL  # 不改状态
    finally:
        await _cleanup(dids)


async def test_repost_count_state_machine():
    dids = []
    reals = await fetch_real_dyns(2)
    try:
        async with new_session() as s:
            src = await _commit_seed(s, D_MID, real=reals[0], dyn_type=MomentTypeEnum.WORD)
            dids.append(src)
            # 转发创建：源动态 repostCount 不 +1（状态机触发点⑥）
            fwd = await _new_moment_id()
            now = _BASE
            s.add(TMoment(dynId=fwd, mid=D_MID2, dynType=MomentTypeEnum.FORWARD, contentText=reals[1].content_text,
                           contentJson=[{"type": "WORDS", "text": reals[1].content_text}], repostSrcDynId=src,
                           auditStatus=MomentAuditStatusEnum.AUDITING, created_at=now, updated_at=now))
            await s.flush()
            s.add(TInteractionStat(bizType=InteractionBizTypeEnum.DYNAMIC, bizId=fwd))
            s.add(
                TResourceFeed(
                    bizType=InteractionBizTypeEnum.DYNAMIC,
                    bizId=fwd,
                    mid=D_MID2,
                    pubTime=None,
                    auditStatus="auditing",
                    tags=[],
                )
            )
            await s.commit()
            dids.append(fwd)

            async def _src_stat():
                return (
                    await s.exec(
                        select(TInteractionStat).where(
                            col(TInteractionStat.bizType) == InteractionBizTypeEnum.DYNAMIC,
                            col(TInteractionStat.bizId) == src,
                        )
                    )
                ).one()

            # 审核通过：源动态 +1
            await MomentStatService.incr_repost_count(s, src, 1)
            stat = await _src_stat()
            assert stat.repostCount == 1
            # 驳回：源动态 -1（带 >0 兜底）
            await MomentStatService.incr_repost_count(s, src, -1)
            stat = await _src_stat()
            assert stat.repostCount == 0
            # 负数兜底：再 -1 不应成负
            await MomentStatService.incr_repost_count(s, src, -1)
            stat = await _src_stat()
            assert stat.repostCount == 0
    finally:
        await _cleanup(dids)


__all__ = [
    "test_report_writes_and_keeps_status",
    "test_repost_count_state_machine",
    "test_thumb_cancel",
    "test_thumb_first_like",
    "test_thumb_idempotent",
    "test_thumb_reject_non_normal",
    "test_view_dedup",
]
