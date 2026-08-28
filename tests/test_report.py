"""统一举报系统测试（P11-T7，2.14.0）。

覆盖：
- `ReportBaseService.record_report` 幂等（一人一对象一次）；
- 统一 `ReportService.report`：动态→`TResourceReport`、评论→`CommentReport`、用户空间→`TUserReport`
  三类写入对应子表；pics 非法 / biz 对象不存在校验；
- 动态举报达阈值（`report_threshold`）仅「加入审核队列」（资源状态不变，2.40.0）；
- 管理端 `list_reports` / `review`。

测试用独立 mid / dynId 区间，避免与既有用例互相干扰；用例结束清理数据。
"""

import pytest
from sqlalchemy import func
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlmodel import col, select, text
from sqlmodel.ext.asyncio.session import AsyncSession as SQLModelAsyncSession

from app.core import database as db_mod
from app.core.config import settings
from app.core.database import new_session
from app.models.db import EventMessage, TResourceFeed
from app.models.db.comment import CommentReport
from app.models.db.moment import TMoment, TResourceReport
from app.models.db.report import TUserReport
from app.models.enums import (
    CommentTypeEnum,
    EventTypeEnum,
    InteractionBizTypeEnum,
    MomentAuditStatusEnum,
    MomentTypeEnum,
)
from app.models.schemas import CommentAddReq, ReportCreateReq, ReportReviewReq
from app.services.message.comment import CommentService
from app.services.admin.report import ReportService
from bili_common.models.report import ReportBizTypeEnum
from bili_common.services.report import ReportBaseService

_OID = 884_000_100_000
_MID = 906_100
_UP = 906_101
# 多个举报人（达阈值需要不同 reportMid）
REP_A = 906_102
REP_B = 906_103
REP_C = 906_104
_TABLES = ("TResourceReport", "msg_comment_report", "TUserReport")
_seq = 0


@pytest.fixture(autouse=True)
async def _bind_engines():
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


def _next_oid() -> int:
    global _seq
    _seq += 1
    return _OID + _seq


async def _cleanup(oid: int) -> None:
    async with new_session() as s:
        for t in _TABLES:
            await s.exec(
                text(f"DELETE FROM {t} WHERE bizId = {oid}")
            )
        await s.exec(text(f"DELETE FROM TResourceFeed WHERE bizId = {oid}"))
        await s.exec(text(f"DELETE FROM TMoment WHERE dynId = {oid}"))
        # 处置/审核通知事件（作者/举报人/审核员区间）
        await s.exec(
            text(
                f"DELETE FROM msg_event WHERE mid IN ({_MID}, {REP_A}, {REP_B}, {REP_C}) "
                f"OR actor_mid IN ({REP_A}, {REP_B}, {REP_C})"
            )
        )
        await s.commit()


async def _create_moment(session, oid: int) -> None:
    session.add(
        TMoment(
            dynId=oid,
            mid=_MID,
            dynType=MomentTypeEnum.WORD,
            contentJson={"nodes": []},
            auditStatus=MomentAuditStatusEnum.NORMAL,
        )
    )
    await session.commit()


async def _real_uid() -> int:
    from sqlalchemy import select as sa_select

    from app.models.pptr_db import PptrUserInfo

    async with db_mod.new_pptr_session() as s:
        row = (
            await s.exec(
                sa_select(PptrUserInfo.uid)
                .where(PptrUserInfo.deletedAt.is_(None))
                .order_by(func.random())
                .limit(1)
            )
        ).one_or_none()
    assert row is not None, "pptr 库无可用真实用户"
    return int(row[0])


async def _create_comment(session, oid: int) -> int:
    r = await CommentService.add(
        session,
        _MID,
        CommentAddReq(
            oid=str(oid),
            type=CommentTypeEnum.DYNAMIC,
            root="0",
            parent="0",
            message="测试评论",
            up_mid=_UP,
        ),
    )
    return int(r.rpid)


# ==================== 幂等 / 子表写入 ====================


async def test_record_report_idempotent():
    """`ReportBaseService.record_report` 幂等：同一用户同一对象第二次 created=False。"""
    async with new_session() as s:
        created1, pk1 = await ReportBaseService.record_report(
            s, TUserReport,
            reporter_mid=REP_A, biz_type=ReportBizTypeEnum.USER.value,
            biz_id=27, accused_mid=27, reason_type=1,
        )
        assert created1 is True and pk1 is not None
        created2, pk2 = await ReportBaseService.record_report(
            s, TUserReport,
            reporter_mid=REP_A, biz_type=ReportBizTypeEnum.USER.value,
            biz_id=27, accused_mid=27, reason_type=1,
        )
        assert created2 is False and pk2 is None
        # 清理
        await s.exec(text(f"DELETE FROM TUserReport WHERE reportMid = {REP_A}"))
        await s.commit()


async def test_report_dynamic_writes_tmoment_report():
    """举报动态 → 写入 TResourceReport（bizType=dynamic）。"""
    oid = _next_oid()
    try:
        async with new_session() as s:
            await _create_moment(s, oid)
        async with new_session() as s:
            created, _ = await ReportService.report(
                s, REP_A,
                ReportCreateReq(bizType="dynamic", bizId=oid, reasonType=1, reasonDesc="动态举报"),
            )
            assert created is True
        async with new_session() as s:
            rows = (
                await s.exec(
                    select(TResourceReport).where(col(TResourceReport.bizId) == oid)
                )
            ).all()
            assert len(rows) == 1
            assert rows[0].bizType == "dynamic"
    finally:
        await _cleanup(oid)


async def test_review_resolve_hide_hides_moment():
    """2.38.0：审核 resolve + resourceAction=hide → 被举报动态下架（TMoment hidden）。"""
    oid = _next_oid()
    try:
        async with new_session() as s:
            await _create_moment(s, oid)
        async with new_session() as s:
            created, _ = await ReportService.report(
                s, REP_A, ReportCreateReq(bizType="dynamic", bizId=oid, reasonType=1)
            )
            assert created is True
            pk = (
                await s.exec(
                    select(TResourceReport.pk).where(
                        col(TResourceReport.bizId) == oid,
                        col(TResourceReport.reportMid) == REP_A,
                    )
                )
            ).one()
        async with new_session() as s:
            await ReportService.review(
                s, REP_B,
                ReportReviewReq(
                    reportPk=int(pk),
                    decision="resolve",
                    resourceAction="hide",
                    remark="违规下架",
                ),
            )
        async with new_session() as s:
            dyn = (
                await s.exec(select(TMoment).where(col(TMoment.dynId) == oid))
            ).one()
            assert dyn.auditStatus is MomentAuditStatusEnum.HIDDEN  # 管理员下架
            rec = (
                await s.exec(
                    select(TResourceReport).where(
                        col(TResourceReport.bizId) == oid,
                        col(TResourceReport.reportMid) == REP_A,
                    )
                )
            ).one()
            assert rec.auditStatus == "resolved"  # 举报已处置
            # 2.38.0：作者（mid 非空）收到 HIDE 下架通知
            evs = (
                await s.exec(
                    select(EventMessage).where(
                        EventMessage.mid == _MID,
                        EventMessage.event_type == EventTypeEnum.HIDE,
                    )
                )
            ).all()
            assert len(evs) >= 1
            assert evs[0].actor_mid == REP_B
    finally:
        await _cleanup(oid)


async def test_report_resource_lottery_not_hideable():
    """2.40.0：lottery（crawler 资源）举报 + resolve/hide → **拒绝下架**（Feed 保持 normal）。"""
    oid = _next_oid()
    try:
        async with new_session() as s:
            now = __import__("datetime").datetime.now()
            s.add(
                TResourceFeed(
                    bizType=InteractionBizTypeEnum.LOTTERY,
                    bizId=oid,
                    mid=_MID,
                    pubTime=now,
                    auditStatus="normal",
                    tags=[],
                )
            )
            await s.commit()
        async with new_session() as s:
            created, _ = await ReportService.report(
                s, REP_A,
                ReportCreateReq(
                    bizType="resource", bizId=oid,
                    resourceType=int(InteractionBizTypeEnum.LOTTERY),
                    reasonType=1,
                ),
            )
            assert created is True
            pk = (
                await s.exec(
                    select(TResourceReport.pk).where(
                        col(TResourceReport.bizId) == oid,
                        col(TResourceReport.reportMid) == REP_A,
                    )
                )
            ).one()
        async with new_session() as s:
            await ReportService.review(
                s, REP_B,
                ReportReviewReq(
                    reportPk=int(pk), decision="resolve", resourceAction="hide"
                ),
            )
        async with new_session() as s:
            feed = (
                await s.exec(
                    select(TResourceFeed).where(
                        col(TResourceFeed.bizType) == InteractionBizTypeEnum.LOTTERY,
                        col(TResourceFeed.bizId) == oid,
                    )
                )
            ).one()
            assert feed.auditStatus == "normal"  # 不允许下架 → 仍在 Feed
            rec = (
                await s.exec(
                    select(TResourceReport).where(
                        col(TResourceReport.bizId) == oid,
                        col(TResourceReport.reportMid) == REP_A,
                    )
                )
            ).one()
            assert rec.auditStatus == "resolved"  # 举报已处置（仅记录）
    finally:
        await _cleanup(oid)


async def test_report_resource_rpa_hide_exits_feed():
    """2.40.0：rpa_* 资源举报 + resolve/hide → Feed 层退出（允许下架）。"""
    oid = _next_oid()
    try:
        async with new_session() as s:
            now = __import__("datetime").datetime.now()
            s.add(
                TResourceFeed(
                    bizType=InteractionBizTypeEnum.RPA_ACTION,
                    bizId=oid,
                    mid=_MID,
                    pubTime=now,
                    auditStatus="normal",
                    tags=[],
                )
            )
            await s.commit()
        async with new_session() as s:
            created, _ = await ReportService.report(
                s, REP_A,
                ReportCreateReq(
                    bizType="resource", bizId=oid,
                    resourceType=int(InteractionBizTypeEnum.RPA_ACTION),
                    reasonType=1,
                ),
            )
            assert created is True
            pk = (
                await s.exec(
                    select(TResourceReport.pk).where(
                        col(TResourceReport.bizId) == oid,
                        col(TResourceReport.reportMid) == REP_A,
                    )
                )
            ).one()
        async with new_session() as s:
            await ReportService.review(
                s, REP_B,
                ReportReviewReq(
                    reportPk=int(pk), decision="resolve", resourceAction="hide"
                ),
            )
        async with new_session() as s:
            feed = (
                await s.exec(
                    select(TResourceFeed).where(
                        col(TResourceFeed.bizType) == InteractionBizTypeEnum.RPA_ACTION,
                        col(TResourceFeed.bizId) == oid,
                    )
                )
            ).one()
            assert feed.auditStatus == "hidden"  # rpa 允许下架 → 退出 Feed
    finally:
        await _cleanup(oid)


async def test_report_list_aggregates_report_count():
    """2.40.0：管理端列表 ReportItem.reportCount 聚合同对象累计举报次数。"""
    oid = _next_oid()
    try:
        async with new_session() as s:
            await _create_moment(s, oid)
        async with new_session() as s:
            for mid in (REP_A, REP_B, REP_C):
                created, _ = await ReportService.report(
                    s, mid,
                    ReportCreateReq(bizType="dynamic", bizId=oid, reasonType=1),
                )
                assert created is True
        async with new_session() as s:
            resp = await ReportService.list_reports(
                s, biz_type="dynamic", page=1, page_size=50
            )
            targets = [it for it in resp.items if it.bizId == oid]
            assert targets and targets[0].reportCount >= 3  # 被举报次数
            assert targets[0].reportPeopleCount >= 3  # 举报人数（去重）
    finally:
        await _cleanup(oid)


async def test_report_review_reject_notifies_reporter():
    """2.40.0：审核 reject（举报不成立）→ 举报记录移出队列 + 通知举报人。"""
    oid = _next_oid()
    try:
        async with new_session() as s:
            await _create_moment(s, oid)
        async with new_session() as s:
            created, _ = await ReportService.report(
                s, REP_A, ReportCreateReq(bizType="dynamic", bizId=oid, reasonType=1)
            )
            assert created is True
            pk = (
                await s.exec(
                    select(TResourceReport.pk).where(
                        col(TResourceReport.bizId) == oid,
                        col(TResourceReport.reportMid) == REP_A,
                    )
                )
            ).one()
        async with new_session() as s:
            await ReportService.review(
                s, REP_B,
                ReportReviewReq(reportPk=int(pk), decision="reject", remark="举报不成立"),
            )
        async with new_session() as s:
            rec = (
                await s.exec(
                    select(TResourceReport).where(
                        col(TResourceReport.bizId) == oid,
                        col(TResourceReport.reportMid) == REP_A,
                    )
                )
            ).one()
            assert rec.auditStatus == "rejected"  # 移出待处理队列
            evs = (
                await s.exec(
                    select(EventMessage).where(
                        EventMessage.mid == REP_A,
                        EventMessage.event_type == EventTypeEnum.REPORT_REJECT,
                    )
                )
            ).all()
            assert len(evs) >= 1  # 通知举报人"举报未通过"
            assert evs[0].actor_mid == REP_B
    finally:
        await _cleanup(oid)


async def test_report_review_resolve_notifies_reporter():
    """2.40.0：审核 resolve（举报成立）→ 通知举报人（REPORT_RESOLVED）。"""
    oid = _next_oid()
    try:
        async with new_session() as s:
            await _create_moment(s, oid)
        async with new_session() as s:
            created, _ = await ReportService.report(
                s, REP_A, ReportCreateReq(bizType="dynamic", bizId=oid, reasonType=1)
            )
            assert created is True
            pk = (
                await s.exec(
                    select(TResourceReport.pk).where(
                        col(TResourceReport.bizId) == oid,
                        col(TResourceReport.reportMid) == REP_A,
                    )
                )
            ).one()
        async with new_session() as s:
            await ReportService.review(
                s, REP_B,
                ReportReviewReq(reportPk=int(pk), decision="resolve", remark="成立"),
            )
        async with new_session() as s:
            rec = (
                await s.exec(
                    select(TResourceReport).where(
                        col(TResourceReport.bizId) == oid,
                        col(TResourceReport.reportMid) == REP_A,
                    )
                )
            ).one()
            assert rec.auditStatus == "resolved"
            evs = (
                await s.exec(
                    select(EventMessage).where(
                        EventMessage.mid == REP_A,
                        EventMessage.event_type == EventTypeEnum.REPORT_RESOLVED,
                    )
                )
            ).all()
            assert len(evs) >= 1  # 通知举报人"举报成立"
            assert evs[0].actor_mid == REP_B
    finally:
        await _cleanup(oid)


async def test_report_user_writes_tuser_report():
    """举报用户空间 → 写入 TUserReport（bizType=user）。"""
    target = await _real_uid()
    try:
        async with new_session() as s:
            created, _ = await ReportService.report(
                s, REP_A,
                ReportCreateReq(bizType="user", bizId=target, reasonType=7, reasonDesc="用户举报"),
            )
            assert created is True
        async with new_session() as s:
            rows = (
                await s.exec(
                    select(TUserReport).where(
                        col(TUserReport.bizId) == target,
                        col(TUserReport.reportMid) == REP_A,
                    )
                )
            ).all()
            assert len(rows) == 1
            assert rows[0].bizType == "user"
    finally:
        async with new_session() as s:
            await s.exec(text(f"DELETE FROM TUserReport WHERE bizId = {target} AND reportMid = {REP_A}"))
            await s.commit()


async def test_report_comment_writes_comment_report():
    """举报评论 → 写入 CommentReport（bizType=comment）。"""
    oid = _next_oid()
    try:
        async with new_session() as s:
            await _create_moment(s, oid)
            rpid = await _create_comment(s, oid)
        async with new_session() as s:
            created, _ = await ReportService.report(
                s, REP_A,
                ReportCreateReq(bizType="comment", bizId=rpid, reasonType=3),
            )
            assert created is True
        async with new_session() as s:
            rows = (
                await s.exec(
                    select(CommentReport).where(
                        col(CommentReport.bizId) == rpid,
                        col(CommentReport.reportMid) == REP_A,
                    )
                )
            ).all()
            assert len(rows) == 1
            assert rows[0].bizType == "comment"
    finally:
        async with new_session() as s:
            await s.exec(text(f"DELETE FROM TMoment WHERE dynId = {oid}"))
            await s.commit()


# ==================== 校验 ====================


async def test_report_pics_validation():
    """非法 pics 抛错（非 http(s) 链接 / 超过 3 张）。"""
    async with new_session() as s:
        with pytest.raises(ValueError):
            await ReportService.report(
                s, REP_A,
                ReportCreateReq(bizType="user", bizId=27, reasonType=1, pics=["not-a-url"]),
            )
        with pytest.raises(ValueError):
            await ReportService.report(
                s, REP_A,
                ReportCreateReq(
                    bizType="user", bizId=27, reasonType=1,
                    pics=["https://a.com/1.jpg", "https://a.com/2.jpg", "https://a.com/3.jpg", "https://a.com/4.jpg"],
                ),
            )


async def test_report_missing_dynamic():
    """举报不存在的动态抛错。"""
    async with new_session() as s:
        with pytest.raises(ValueError):
            await ReportService.report(
                s, REP_A, ReportCreateReq(bizType="dynamic", bizId=999_999_999_999, reasonType=1)
            )


# ==================== 阈值联动 ====================


async def test_report_dynamic_threshold_linkage():
    """2.40.0：动态举报达阈值（3）仅加入审核队列——资源状态不变（不下架）。"""
    oid = _next_oid()
    try:
        async with new_session() as s:
            await _create_moment(s, oid)
        # 3 个不同用户举报同一动态
        for reporter in (REP_A, REP_B, REP_C):
            async with new_session() as s:
                created, triggered = await ReportService.report(
                    s, reporter, ReportCreateReq(bizType="dynamic", bizId=oid, reasonType=2)
                )
                assert created is True
            if reporter == REP_C:
                assert triggered is True  # 第 3 次达阈值（仅队列标记）
        async with new_session() as s:
            dyn = (
                await s.exec(select(TMoment).where(col(TMoment.dynId) == oid))
            ).one()
            # 2.40.0：达阈值不下架、不转审核——资源状态保持 normal，等管理员在队列中决定
            assert dyn.auditStatus == MomentAuditStatusEnum.NORMAL
    finally:
        await _cleanup(oid)


# ==================== 管理端 ====================


async def test_report_list_review():
    """管理端 list / review。"""
    oid = _next_oid()
    try:
        async with new_session() as s:
            await _create_moment(s, oid)
            await ReportService.report(s, REP_A, ReportCreateReq(bizType="dynamic", bizId=oid, reasonType=1))
        async with new_session() as s:
            data = await ReportService.list_reports(s, biz_type="dynamic")
            assert data.total >= 1
            target = next((it for it in data.items if it.bizId == oid), None)
            assert target is not None
            assert target.auditStatus == "pending"
            await ReportService.review(
                s, 1,
                ReportReviewReq(reportPk=target.pk, decision="resolve", remark="属实"),
            )
        async with new_session() as s:
            data = await ReportService.list_reports(s, status="resolved")
            target = next((it for it in data.items if it.bizId == oid), None)
            assert target is not None
            assert target.auditStatus == "resolved"
            assert target.auditAdminMid == 1
    finally:
        await _cleanup(oid)
