"""Phase 6 — 动态审核服务单元测试（P6-T1 ~ P6-T4）。

覆盖：

- 待审核列表（P6-T1）：auditStatus=auditing 进入列表；normal 不进入。
- 审核通过（P6-T2）：auditing→normal + pubTime 写入；FORWARD 时源动态 repostCount +1；
  不发通知（无 AUDIT_REJECT 事件）。
- 审核驳回（P6-T3）：auditing→rejected + 驳回原因；向作者投递 AUDIT_REJECT 事件；
  FORWARD ∧ before=normal 时源动态 repostCount -1。
- 审核流水（P6-T4）：按 dynId 过滤返回对应流转记录。
- 单条详情（P6-T5）：返回当前快照 + 历史流转。

复用真实 MySQL 主库，独立 mid 区间避免与既有用例冲突。
"""

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlmodel import col, select, text
from sqlmodel.ext.asyncio.session import AsyncSession as SQLModelAsyncSession

from app.core import database as db_mod
from app.core.config import settings
from app.core.database import new_session
from app.core.sharding import generate_moment_id
from app.models.db import EventMessage, TMoment, TInteractionStat, TResourceFeed
from bili_common.models import InteractionActionTypeEnum, InteractionBizTypeEnum
from app.models.enums import (
    MomentAuditLogActionEnum,
    ResourceAuditStatusEnum,
    MomentTypeEnum,
)
from app.services.interaction_actions import get_biz
from app.services.moment.moment_audit import MomentAuditService
from seed_biliopus import fetch_real_dyns

# 独立区间，避免与 Phase 2/5 用例冲突
A_MID = 930001
A_MID2 = 930002
ADMIN_MID = 930099


async def _real_content() -> str:
    """从 biliopusdb 拉取一条真实动态正文作为测试种子内容。"""
    reals = await fetch_real_dyns(1)
    return reals[0].content_text if reals else "seed"


@pytest.fixture(autouse=True)
async def _bind_engine_per_test():
    """每个测试重建 engine（含 pptr），并按本模块专属 mid 区间预清理，保证幂等可重复。

    审核服务内部会回查 pptr 用户（PptrUser.get_many），而模块级单例 pptr
    engine 会绑到首个事件循环，跨 loop 复用会报 "attached to a different loop"，
    因此 pptr engine 也需随测试重建并绑定当前 loop（同 test_moment_feed）。
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

    from app.core import database as db_mod_pptr

    # 保存原始全局 pptr 引擎，teardown 时还原，避免本模块 dispose 后
    # 污染后续不重绑 pptr 的测试文件（如 test_comment_crud）。
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
            text(
                f"DELETE FROM TResourceFeed WHERE bizType = 1 AND bizId IN "
                f"(SELECT dynId FROM TMoment WHERE mid IN ({A_MID}, {A_MID2}))"
            )
        )
        await s.exec(
            text(
                f"DELETE FROM TInteractionStat WHERE bizType = 1 AND bizId IN "
                f"(SELECT dynId FROM TMoment WHERE mid IN ({A_MID}, {A_MID2}))"
            )
        )
        await s.exec(text(f"DELETE FROM TMoment WHERE mid IN ({A_MID}, {A_MID2})"))
        await s.exec(
            text(
                f"DELETE FROM msg_event WHERE mid IN ({A_MID}, {A_MID2}) "
                f"OR actor_mid IN ({A_MID}, {A_MID2}, {ADMIN_MID})"
            )
        )
        await s.commit()
    yield
    # teardown 同样清理，避免本模块在共享测试库留下 dynId 残留，
    # 污染后续测试文件（分钟步进雪花 ID 同分钟内序列号有限，可能主键撞号）。
    async with new_session() as s:
        await s.exec(
            text(
                f"DELETE FROM TResourceFeed WHERE bizType = 1 AND bizId IN "
                f"(SELECT dynId FROM TMoment WHERE mid IN ({A_MID}, {A_MID2}))"
            )
        )
        await s.exec(
            text(
                f"DELETE FROM TInteractionStat WHERE bizType = 1 AND bizId IN "
                f"(SELECT dynId FROM TMoment WHERE mid IN ({A_MID}, {A_MID2}))"
            )
        )
        await s.exec(text(f"DELETE FROM TMoment WHERE mid IN ({A_MID}, {A_MID2})"))
        await s.exec(
            text(
                f"DELETE FROM msg_event WHERE mid IN ({A_MID}, {A_MID2}) "
                f"OR actor_mid IN ({A_MID}, {A_MID2}, {ADMIN_MID})"
            )
        )
        await s.commit()
    await engine.dispose()
    await pptr_engine.dispose()
    # 还原全局 pptr 引擎，保持对后续测试文件的透明（test_comment_crud 等不重绑）
    db_mod_pptr.pptr_engine = orig_pptr_engine
    db_mod_pptr.pptr_session_maker = orig_pptr_session_maker


async def _seed_moment(
    session,
    mid: int,
    *,
    dyn_type: MomentTypeEnum = MomentTypeEnum.WORD,
    audit_status: ResourceAuditStatusEnum = ResourceAuditStatusEnum.AUDITING,
    repost_src_dyn_id: int | None = None,
    content: str | None = None,
    with_stat: bool = True,
) -> int:
    did = await generate_moment_id()
    now = __import__("datetime").datetime.now()
    content = content if content is not None else await _real_content()
    dyn = TMoment(
        dynId=did,
        mid=mid,
        dynType=dyn_type,
        contentText=content,
        contentJson=[{"type": "WORDS", "text": content}],
        repostSrcDynId=repost_src_dyn_id,
        auditStatus=audit_status,
        pubTime=now if audit_status is ResourceAuditStatusEnum.NORMAL else None,
        created_at=now,
        updated_at=now,
    )
    session.add(dyn)
    await session.flush()
    if with_stat:
        # 2.36.0：计数统一 TInteractionStat
        session.add(
            TInteractionStat(bizType=InteractionBizTypeEnum.DYNAMIC, bizId=did)
        )
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


async def _set_src_repost_count(session, src_id: int, value: int) -> None:
    stat = (
        await session.exec(
            select(TInteractionStat).where(
                col(TInteractionStat.bizType) == InteractionBizTypeEnum.DYNAMIC,
                col(TInteractionStat.bizId) == src_id,
            )
        )
    ).one_or_none()
    assert stat is not None, "源动态统计行必须存在"
    stat.repostCount = value
    await session.commit()


async def _get_repost_count(session, src_id: int) -> int:
    stat = (
        await session.exec(
            select(TInteractionStat).where(
                col(TInteractionStat.bizType) == InteractionBizTypeEnum.DYNAMIC,
                col(TInteractionStat.bizId) == src_id,
            )
        )
    ).one_or_none()
    return stat.repostCount if stat else 0


# ==================== P6-T1 待审核列表 ====================


async def test_pending_list_only_auditing():
    async with new_session() as s:
        m_audit = await _seed_moment(
            s, A_MID, audit_status=ResourceAuditStatusEnum.AUDITING
        )
        m_normal = await _seed_moment(
            s, A_MID, audit_status=ResourceAuditStatusEnum.NORMAL
        )

        resp = await MomentAuditService.pending_list(s, page_num=1, page_size=20)
        assert isinstance(resp, object)
        ids = {it.dynId for it in resp.items}
        assert m_audit in ids
        assert m_normal not in ids
        assert resp.total >= 1


# ==================== P6-T2 审核通过 ====================


async def test_approve_sets_normal_and_pubtime():
    async with new_session() as s:
        did = await _seed_moment(
            s, A_MID, audit_status=ResourceAuditStatusEnum.AUDITING
        )
        item = await get_biz(InteractionBizTypeEnum.DYNAMIC, s, did, ADMIN_MID).audit_approve(remark="ok")
        assert item.auditStatus == ResourceAuditStatusEnum.NORMAL.name
        assert item.pubTime is not None

        dyn = (
            await s.exec(select(TMoment).where(TMoment.dynId == did))
        ).one_or_none()
        assert dyn.auditStatus is ResourceAuditStatusEnum.NORMAL
        assert dyn.pubTime is not None
        assert dyn.auditRejectReason is None


async def test_approve_forward_increments_src_repost_count():
    async with new_session() as s:
        src = await _seed_moment(s, A_MID2, audit_status=ResourceAuditStatusEnum.NORMAL)
        await _set_src_repost_count(s, src, 5)
        fwd = await _seed_moment(
            s,
            A_MID,
            dyn_type=MomentTypeEnum.FORWARD,
            audit_status=ResourceAuditStatusEnum.AUDITING,
            repost_src_dyn_id=src,
        )

        await get_biz(InteractionBizTypeEnum.DYNAMIC, s, fwd, ADMIN_MID).audit_approve()
        assert await _get_repost_count(s, src) == 6  # 触发点①：+1


async def test_approve_does_not_fire_reject_event():
    async with new_session() as s:
        did = await _seed_moment(
            s, A_MID, audit_status=ResourceAuditStatusEnum.AUDITING
        )
        await get_biz(InteractionBizTypeEnum.DYNAMIC, s, did, ADMIN_MID).audit_approve()

    # 审核通过禁止发通知：AUDIT_REJECT 事件不应产生
    async with new_session() as s2:
        rows = (
            await s2.exec(
                select(EventMessage).where(
                    EventMessage.mid == A_MID,
                    EventMessage.event_type == InteractionActionTypeEnum.AUDIT_REJECT,
                )
            )
        ).all()
        assert rows == []


# ==================== P6-T3 审核驳回 ====================


async def test_reject_sets_rejected_and_reason():
    async with new_session() as s:
        did = await _seed_moment(
            s, A_MID, audit_status=ResourceAuditStatusEnum.AUDITING
        )
        item = await get_biz(InteractionBizTypeEnum.DYNAMIC, s, did, ADMIN_MID).audit_reject(
            reject_reason="违规内容", remark="r"
        )
        assert item.auditStatus == ResourceAuditStatusEnum.REJECTED.name

        dyn = (
            await s.exec(select(TMoment).where(TMoment.dynId == did))
        ).one_or_none()
        assert dyn.auditStatus is ResourceAuditStatusEnum.REJECTED
        assert dyn.auditRejectReason == "违规内容"


async def test_reject_fires_audit_reject_event_to_author():
    async with new_session() as s:
        did = await _seed_moment(
            s, A_MID, audit_status=ResourceAuditStatusEnum.AUDITING
        )
        await get_biz(InteractionBizTypeEnum.DYNAMIC, s, did, ADMIN_MID).audit_reject(
            reject_reason="违规内容"
        )

    # 弱依赖事件：应投递给作者（接收者 mid = 作者）
    async with new_session() as s2:
        rows = (
            await s2.exec(
                select(EventMessage).where(
                    EventMessage.mid == A_MID,
                    EventMessage.event_type == InteractionActionTypeEnum.AUDIT_REJECT,
                )
            )
        ).all()
        assert len(rows) >= 1
        assert rows[0].actor_mid == ADMIN_MID


async def test_reject_forward_normal_decrements_src_repost_count():
    async with new_session() as s:
        # before=normal 的 FORWARD，驳回应触发 -1
        src = await _seed_moment(s, A_MID2, audit_status=ResourceAuditStatusEnum.NORMAL)
        await _set_src_repost_count(s, src, 5)
        fwd = await _seed_moment(
            s,
            A_MID,
            dyn_type=MomentTypeEnum.FORWARD,
            audit_status=ResourceAuditStatusEnum.NORMAL,
            repost_src_dyn_id=src,
        )

        await get_biz(InteractionBizTypeEnum.DYNAMIC, s, fwd, ADMIN_MID).audit_reject(
            reject_reason="撤回"
        )
        assert await _get_repost_count(s, src) == 4  # 触发点②：-1


async def test_reject_auditing_forward_no_decrement():
    async with new_session() as s:
        # before=auditing 的 FORWARD，驳回不应触发源动态 -1
        src = await _seed_moment(s, A_MID2, audit_status=ResourceAuditStatusEnum.NORMAL)
        await _set_src_repost_count(s, src, 5)
        fwd = await _seed_moment(
            s,
            A_MID,
            dyn_type=MomentTypeEnum.FORWARD,
            audit_status=ResourceAuditStatusEnum.AUDITING,
            repost_src_dyn_id=src,
        )

        await get_biz(InteractionBizTypeEnum.DYNAMIC, s, fwd, ADMIN_MID).audit_reject(
            reject_reason="撤回"
        )
        assert await _get_repost_count(s, src) == 5  # 未触发


# ==================== P27 按状态筛选（2.30.0）====================


async def test_pending_list_filters_by_status():
    """2.30.0：pending_list 支持按 audit_status 筛选，各状态互不串扰。"""
    async with new_session() as s:
        m_audit = await _seed_moment(
            s, A_MID, audit_status=ResourceAuditStatusEnum.AUDITING
        )
        m_normal = await _seed_moment(
            s, A_MID, audit_status=ResourceAuditStatusEnum.NORMAL
        )
        m_rejected = await _seed_moment(
            s, A_MID, audit_status=ResourceAuditStatusEnum.REJECTED
        )

        audit_resp = await MomentAuditService.pending_list(
            s, audit_status=ResourceAuditStatusEnum.AUDITING
        )
        normal_resp = await MomentAuditService.pending_list(
            s, audit_status=ResourceAuditStatusEnum.NORMAL
        )
        rejected_resp = await MomentAuditService.pending_list(
            s, audit_status=ResourceAuditStatusEnum.REJECTED
        )

        audit_ids = {it.dynId for it in audit_resp.items}
        normal_ids = {it.dynId for it in normal_resp.items}
        rejected_ids = {it.dynId for it in rejected_resp.items}

        assert m_audit in audit_ids and m_audit not in normal_ids and m_audit not in rejected_ids
        assert m_normal in normal_ids and m_normal not in audit_ids and m_normal not in rejected_ids
        assert m_rejected in rejected_ids and m_rejected not in audit_ids and m_rejected not in normal_ids


async def test_reject_normal_word_moment_reverts():
    """失误过审撤回：normal 的 WORD 动态可驳回为 rejected（写原因 + 流水），
    并立即从 normal 列表消失、进入 rejected 列表。"""
    async with new_session() as s:
        did = await _seed_moment(
            s, A_MID, audit_status=ResourceAuditStatusEnum.NORMAL
        )
        item = await get_biz(InteractionBizTypeEnum.DYNAMIC, s, did, ADMIN_MID).audit_reject(
            reject_reason="误过审，撤回"
        )
        assert item.auditStatus == ResourceAuditStatusEnum.REJECTED.name

        dyn = (
            await s.exec(select(TMoment).where(TMoment.dynId == did))
        ).one_or_none()
        assert dyn.auditStatus is ResourceAuditStatusEnum.REJECTED
        assert dyn.auditRejectReason == "误过审，撤回"

        normal_ids = {
            it.dynId
            for it in (
                await MomentAuditService.pending_list(
                    s, audit_status=ResourceAuditStatusEnum.NORMAL
                )
            ).items
        }
        rejected_ids = {
            it.dynId
            for it in (
                await MomentAuditService.pending_list(
                    s, audit_status=ResourceAuditStatusEnum.REJECTED
                )
            ).items
        }
        assert did not in normal_ids
        assert did in rejected_ids


# ==================== P6-T4 审核流水 ====================


async def test_log_list_records_transition():
    async with new_session() as s:
        did = await _seed_moment(
            s, A_MID, audit_status=ResourceAuditStatusEnum.AUDITING
        )
        await get_biz(InteractionBizTypeEnum.DYNAMIC, s, did, ADMIN_MID).audit_approve()

        resp = await MomentAuditService.log_list(s, dyn_id=did)
        actions = [it.actionType for it in resp.items]
        assert MomentAuditLogActionEnum.APPROVE.value in actions
        assert resp.total >= 1


# ==================== P6-T5 单条详情 ====================


async def test_detail_returns_snapshot_and_logs():
    async with new_session() as s:
        did = await _seed_moment(
            s, A_MID, audit_status=ResourceAuditStatusEnum.AUDITING
        )
        await get_biz(InteractionBizTypeEnum.DYNAMIC, s, did, ADMIN_MID).audit_reject(
            reject_reason="违规"
        )

        detail = await MomentAuditService.detail(s, did)
        assert detail.item is not None
        assert detail.item.auditStatus == ResourceAuditStatusEnum.REJECTED.name
        assert len(detail.logs) >= 1
        assert detail.logs[0].actionType == MomentAuditLogActionEnum.REJECT.value


__all__ = [
    "test_approve_does_not_fire_reject_event",
    "test_approve_forward_increments_src_repost_count",
    "test_approve_sets_normal_and_pubtime",
    "test_detail_returns_snapshot_and_logs",
    "test_log_list_records_transition",
    "test_pending_list_filters_by_status",
    "test_pending_list_only_auditing",
    "test_reject_auditing_forward_no_decrement",
    "test_reject_fires_audit_reject_event_to_author",
    "test_reject_forward_normal_decrements_src_repost_count",
    "test_reject_normal_word_moment_reverts",
    "test_reject_sets_rejected_and_reason",
]
