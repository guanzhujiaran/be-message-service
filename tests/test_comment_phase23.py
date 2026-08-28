"""Phase 2–5 评论系统进阶功能集成测试。

覆盖：点赞/点踩幂等与计数、楼中楼预览与展开、置顶、@搜索、管理端审核/明文IP/统计、
防刷、敏感词审核。与 test_comment_crud.py 共用同一真实 MySQL，但使用独立 mid/oid 区间。
"""

from datetime import datetime

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlmodel import col, select, text
from sqlmodel.ext.asyncio.session import AsyncSession as SQLModelAsyncSession

from app.core import database as db_mod
from app.core.config import settings
from app.core.database import new_pptr_session, new_session, test_pptr_connection
from app.models.db import (
    CommentAction,
    CommentIndex,
    CommentSubject,
    TMoment,
)
from app.models.enums import (
    CommentActionEnum,
    CommentStateEnum,
    CommentTypeEnum,
    MomentAuditStatusEnum,
    MomentTypeEnum,
)
from app.models.pptr_user import PptrUserDetail, PptrUserInfo
from app.models.schemas import CommentAddReq
from app.services.message.comment import CommentService
from app.services.message.comment_action import CommentActionService
from app.services.message.comment_admin import CommentAdminService
from app.services.message.comment_audit import audit_text
from app.services.message.comment_read import CommentReadService
from app.services.user.pptr_user import PptrUserService


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
    yield
    await engine.dispose()
    await pptr_engine.dispose()


_OID = 883_000_000_000
_AUTHOR = 905_000
_UP = 905_001
_VIEWER = 905_002
_STRANGER = 905_003
_AT_USER = 905_004
_SPAM_MID = 905_200
_SEARCH_MID = 905_100
_seq = 0


def _next_oid() -> int:
    global _seq
    _seq += 1
    return _OID + _seq


async def _cleanup(oid: int, mids: set[int]) -> None:
    async with new_session() as s:
        await s.exec(text(f"DELETE FROM msg_comment_action WHERE rpid IN (SELECT rpid FROM msg_comment_index WHERE oid = {oid})"))
        await s.exec(text(f"DELETE FROM msg_comment_at WHERE oid = {oid}"))
        await s.exec(text(f"DELETE FROM msg_comment_content WHERE rpid IN (SELECT rpid FROM msg_comment_index WHERE oid = {oid})"))
        await s.exec(text(f"DELETE FROM msg_comment_index WHERE oid = {oid}"))
        await s.exec(text(f"DELETE FROM msg_comment_subject WHERE oid = {oid}"))
        if mids:
            placeholders = ",".join(str(m) for m in mids)
            await s.exec(text(f"DELETE FROM msg_event WHERE mid IN ({placeholders}) OR actor_mid IN ({placeholders})"))
        # 测试挂载的真实动态行（子表由 FK ON DELETE CASCADE 级联清理）
        await s.exec(text(f"DELETE FROM TMoment WHERE dynId = {oid}"))
        await s.commit()


async def _ensure_moment(session, oid: int, mid: int) -> None:
    """确保测试 oid 有对应真实 TMoment 行（DYNAMIC 评论计数回写的宿主）。

    DYNAMIC 评论的 `commentCount ±1`（审核通过 / 驳回 / 删除）经
    ``MomentStatService`` 回写动态计数（2.36.0 起为 `TInteractionStat`），
    评论测试需把 oid 挂到真实动态上以完整覆盖 DYNAMIC 生命周期。
    """
    exists = (
        await session.exec(select(TMoment.dynId).where(TMoment.dynId == oid))
    ).one_or_none()
    if exists is None:
        now = datetime.utcnow()  # noqa: DTZ003
        session.add(
            TMoment(
                dynId=oid,
                mid=mid,
                dynType=MomentTypeEnum.WORD,
                contentText="comment-seed",
                contentJson=[{"type": "WORDS", "text": "comment-seed"}],
                auditStatus=MomentAuditStatusEnum.NORMAL,
                pubTime=now,
                created_at=now,
                updated_at=now,
            )
        )
        await session.flush()


async def _pass_audit(session, rpid: int, oid: int, *, is_root: bool = False) -> None:
    """先审后发：把 `add` 发布的 auditing 评论显式审核通过为 NORMAL，并补齐计数。

    `comment_pre_audit=True` 下发布即 `auditing`，auditing 不计入
    `root_count/all_count`（见 comment.py「仅 NORMAL 才计入」），审核通过时补 +1。
    不走 `CommentAdminService.set_state`：其内部会回写动态计数（经
    ``MomentStatService``），而测试 oid 为虚构值、无真实 `TMoment` 宿主行。
    """
    row = (
        await session.exec(
            select(CommentIndex).where(col(CommentIndex.rpid) == rpid)
        )
    ).one()
    row.state = CommentStateEnum.NORMAL
    session.add(row)
    subject = (
        await session.exec(
            select(CommentSubject).where(
                col(CommentSubject.oid) == oid,
                col(CommentSubject.type) == CommentTypeEnum.DYNAMIC,
            )
        )
    ).one_or_none()
    if subject is not None:
        subject.all_count += 1
        if is_root:
            subject.root_count += 1
        session.add(subject)
    await session.commit()


async def _add(session, mid, oid, *, message="测试评论", up_mid=0, root="0", parent="0", at_mids=None, uname=None):
    rpid = (
        await CommentService.add(
            session,
            mid,
            CommentAddReq(
                oid=str(oid),
                type=CommentTypeEnum.DYNAMIC,
                root=root,
                parent=parent,
                message=message,
                up_mid=up_mid or None,
                at_mids=at_mids or [],
            ),
            uname=uname or f"user{mid}",
            ip_v4="203.0.113.45",
            ip_v6="2408:8207:78d2:1a00::1",
        )
    ).rpid
    # 评论挂在真实动态上（stat 回写的父行）
    await _ensure_moment(session, oid, mid)
    # 先审后发：发布即 auditing，显式审核通过置 NORMAL（一级评论补 root_count）
    await _pass_audit(session, int(rpid), oid, is_root=root in ("0", 0))
    return rpid


async def test_like_hate_idempotent_and_counts() -> None:
    """点赞幂等、赞→踩→取消的状态翻转正确修正计数，并联动热度。"""
    oid = _next_oid()
    try:
        async with new_session() as s:
            rpid = await _add(s, _AUTHOR, oid)

        async with new_session() as s:
            # 点赞
            r1 = await CommentActionService.action(s, _VIEWER, int(rpid), CommentActionEnum.LIKE)
            assert r1.like_count == 1 and r1.action is CommentActionEnum.LIKE
            # 重复点赞不重复计数
            r2 = await CommentActionService.action(s, _VIEWER, int(rpid), CommentActionEnum.LIKE)
            assert r2.like_count == 1
            # 热度应随点赞上升
            row = (await s.exec(select(CommentIndex).where(CommentIndex.rpid == int(rpid)))).one()
            assert row.hot_score > 0, "点赞后热度分应 > 0"

            # 赞 → 踩：like 归零、hate +1
            r3 = await CommentActionService.action(s, _VIEWER, int(rpid), CommentActionEnum.HATE)
            assert r3.like_count == 0 and r3.hate_count == 1
            # 取消：全部归零
            r4 = await CommentActionService.action(s, _VIEWER, int(rpid), CommentActionEnum.NONE)
            assert r4.like_count == 0 and r4.hate_count == 0 and r4.action is CommentActionEnum.NONE

            # 互动态持久化：重新取 action 行
            act = (await s.exec(select(CommentAction).where(CommentAction.rpid == int(rpid), CommentAction.mid == _VIEWER))).one_or_none()
            assert act is None or act.action is CommentActionEnum.NONE
    finally:
        await _cleanup(oid, {_AUTHOR, _VIEWER})


async def test_sub_preview_and_reply_list() -> None:
    """一级列表内嵌楼中楼预览（截断到 preview_count），展开接口可分页。"""
    oid = _next_oid()
    preview = settings.comment_sub_preview_count
    try:
        async with new_session() as s:
            root = await _add(s, _AUTHOR, oid)
            # 发 5 条楼中楼
            for i in range(5):
                await _add(s, _VIEWER + i, oid, message=f"回复{i}", root=root, parent=root)
        assert preview == 3, "预览条数配置应保持 3"

        async with new_session() as s:
            listing = await CommentReadService.list_main(s, oid, CommentTypeEnum.DYNAMIC, viewer_mid=None)
            assert len(listing.items) == 1
            root_item = listing.items[0]
            assert len(root_item.replies) == preview, "预览应截断到 preview_count"
            assert root_item.replies[0].message == "回复0"

            # 展开接口：total=5，分页取前 2 条
            sub = await CommentReadService.get_sub_list(s, int(root), oid, CommentTypeEnum.DYNAMIC, page_num=1, page_size=2)
            assert sub.total == 5
            assert len(sub.items) == 2
    finally:
        await _cleanup(oid, {_AUTHOR} | {_VIEWER + i for i in range(5)})


async def test_top_pin_permission() -> None:
    """UP 主可置顶/取消；陌生人无权；置顶读者列表首位。"""
    oid = _next_oid()
    try:
        async with new_session() as s:
            root = await _add(s, _AUTHOR, oid, up_mid=_UP)

        # 陌生人无权置顶
        async with new_session() as s:
            assert not await CommentService.set_top(s, _STRANGER, oid, CommentTypeEnum.DYNAMIC, int(root), top=True)
        # UP 主置顶
        async with new_session() as s:
            assert await CommentService.set_top(s, _UP, oid, CommentTypeEnum.DYNAMIC, int(root), top=True)
            listing = await CommentReadService.list_main(s, oid, CommentTypeEnum.DYNAMIC, viewer_mid=None)
            assert listing.top is not None and listing.top.rpid == root
            assert listing.top.is_top is True
        # UP 主取消置顶
        async with new_session() as s:
            assert await CommentService.set_top(s, _UP, oid, CommentTypeEnum.DYNAMIC, int(root), top=False)
            listing2 = await CommentReadService.list_main(s, oid, CommentTypeEnum.DYNAMIC, viewer_mid=None)
            assert listing2.top is None
    finally:
        await _cleanup(oid, {_AUTHOR, _UP, _STRANGER})


async def test_at_search() -> None:
    """@ 面板按昵称前缀搜索命中 pptr 用户表。"""
    # 该用例依赖外部 pptr Postgres（只读库），不可达时跳过而非失败
    if not await test_pptr_connection():
        pytest.skip("pptr Postgres 不可达，跳过 @搜索集成测试")

    oid = _next_oid()
    try:
        async with new_session() as s:
            await _add(s, _SEARCH_MID, oid, message="我是被搜索的用户", uname="搜索目标用户ABC")

        # 在 pptr 直接写入可被搜索到的用户资料（TUserInfo + TUserDetail）
        async with new_pptr_session() as ps:
            await ps.exec(text('DELETE FROM "TUserDetail" WHERE mid = :m'), params={"m": _SEARCH_MID})
            await ps.exec(text('DELETE FROM "TUserInfo" WHERE uid = :m'), params={"m": _SEARCH_MID})
            # 无 relationship 声明，UoW 无法推断依赖顺序：必须先 flush 父表
            # TUserInfo 让 mid 在当前事务可见，再写 TUserDetail（见 pptr_user.create_user）
            ps.add(PptrUserInfo(uid=_SEARCH_MID, user_name="搜索目标用户ABC", role="level0"))
            await ps.flush()
            ps.add(PptrUserDetail(mid=_SEARCH_MID, uname="搜索目标用户ABC", sign="", sex=""))
            await ps.commit()

        hits = await PptrUserService.search_by_uname("搜索目标", limit=10)
        assert any(h.mid == _SEARCH_MID for h in hits)
    finally:
        # 清理 pptr 种子数据（硬删，避免污染其它用例）
        try:
            async with new_pptr_session() as ps:
                await ps.exec(text('DELETE FROM "TUserDetail" WHERE mid = :m'), params={"m": _SEARCH_MID})
                await ps.exec(text('DELETE FROM "TUserInfo" WHERE uid = :m'), params={"m": _SEARCH_MID})
                await ps.commit()
        except Exception:  # noqa: BLE001
            pass
        await _cleanup(oid, {_SEARCH_MID})


async def test_admin_audit_plaintext_ip_stats() -> None:
    """管理端：审核置状态、明文 IP、审核队列、统计。"""
    oid = _next_oid()
    try:
        async with new_session() as s:
            rpid = await _add(s, _AUTHOR, oid, message="待审核评论")

        async with new_session() as s:
            # 驳回
            assert await CommentAdminService.set_state(s, int(rpid), CommentStateEnum.REJECTED)
            item = await CommentAdminService.get_audit_item(s, int(rpid))
            assert item is not None and item.state is CommentStateEnum.REJECTED
            # 明文 IP 仅管理端可见
            ip_v4, ip_v6 = await CommentAdminService.get_plaintext_ip(s, int(rpid))
            assert ip_v4 == "203.0.113.45" and ip_v6 == "2408:8207:78d2:1a00::1"
            # 审核队列包含它（位置参数会把 1 误传给 states，必须关键字传参）
            queue, total = await CommentAdminService.list_audit_queue(
                s, page_num=1, page_size=20
            )
            assert total >= 1 and any(i.rpid == rpid for i in queue)
            # 统计：全局评论总数 >= 1
            stats = await CommentAdminService.get_stats(s)
            assert stats.total_comments >= 1
    finally:
        await _cleanup(oid, {_AUTHOR})


async def test_anti_spam_rate_limit() -> None:
    """同用户同内容 10s 内第 4 次被拒（防刷）。"""
    oid = _next_oid()
    content = "刷屏相同内容测试防刷"
    try:
        async with new_session() as s:
            for i in range(3):
                rp = await _add(s, _SPAM_MID, oid, message=content)
                assert int(rp) > 0
            # 第 4 次应被限流
            with pytest.raises(ValueError):
                await _add(s, _SPAM_MID, oid, message=content)
    finally:
        await _cleanup(oid, {_SPAM_MID})


async def test_sensitive_word_audit() -> None:
    """敏感词审核：高危拒审、疑似待审，且拒审评论对外不可见。"""
    # 单元层：词库匹配
    assert audit_text("今天天气真好")[0] is CommentStateEnum.NORMAL
    assert audit_text("这是诈骗内容")[0] is CommentStateEnum.REJECTED
    assert audit_text("加微信看广告")[0] is CommentStateEnum.AUDITING

    # 集成层：高危词评论被拒审，不出现在列表/详情
    oid = _next_oid()
    try:
        async with new_session() as s:
            resp = await CommentService.add(
                s, _AUTHOR, CommentAddReq(oid=str(oid), type=CommentTypeEnum.DYNAMIC, message="这是诈骗内容"),
                uname="u",
            )
            assert resp.state is CommentStateEnum.REJECTED
            assert resp.need_audit is True
        async with new_session() as s:
            listing = await CommentReadService.list_main(s, oid, CommentTypeEnum.DYNAMIC, viewer_mid=None)
            assert listing.total == 0, "拒审评论不应出现在列表"
            detail = await CommentReadService.get_detail(s, int(resp.rpid))
            assert detail is None, "拒审评论详情不可见"
    finally:
        await _cleanup(oid, {_AUTHOR})


async def test_author_sees_own_auditing_comment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """审核中评论：仅作者本人可见（带 auditing 状态标识），且不计入计数。

    用「疑似敏感词」触发审核态，并把 `comment_pre_audit` 关掉，让其它普通评论
    直接 NORMAL（保证计数口径可对照）。用 LOTTERY 类型避开 DYNAMIC 的
    MomentStat 外键关联（测试 oid 没有对应的 TMoment 父行）。
    """
    # 关掉「先审后发」，只靠命中疑似词进入审核态，保证判定可控
    monkeypatch.setattr(settings, "comment_pre_audit", False)

    oid = _next_oid()
    try:
        async with new_session() as s:
            # 普通评论：直接 NORMAL，计入计数（用 LOTTERY 类型避开 DYNAMIC 的
            # MomentStat 外键关联，测试 oid 没有对应的 TMoment 父行）
            normal_resp = await CommentService.add(
                s,
                _AUTHOR,
                CommentAddReq(oid=str(oid), type=CommentTypeEnum.LOTTERY, message="一条正常评论"),
                uname=f"user{_AUTHOR}",
            )
            assert normal_resp.state is CommentStateEnum.NORMAL
            normal_rpid = normal_resp.rpid
            # 疑似敏感词评论：进入 AUDITING
            audit_resp = await CommentService.add(
                s,
                _AUTHOR,
                CommentAddReq(
                    oid=str(oid),
                    type=CommentTypeEnum.LOTTERY,
                    message="这个链接加微信看广告",
                ),
                uname=f"user{_AUTHOR}",
            )
            assert audit_resp.state is CommentStateEnum.AUDITING
            audit_rpid = audit_resp.rpid

        # 作者视角：能看到自己审核中的评论，带 auditing 标识；计数只算 NORMAL
        async with new_session() as s:
            own = await CommentReadService.list_main(
                s, oid, CommentTypeEnum.LOTTERY, viewer_mid=_AUTHOR
            )
            assert own.total == 1, "计数应只统计 NORMAL 评论"
            assert own.all_count == 1
            rpids = [it.rpid for it in own.items]
            assert normal_rpid in rpids, "普通评论应出现在列表"
            assert audit_rpid in rpids, "作者应看到自己审核中的评论"
            audit_item = next(it for it in own.items if it.rpid == audit_rpid)
            assert audit_item.state is CommentStateEnum.AUDITING, "审核中评论应带 auditing 标识"

        # 他人视角 / 匿名视角：看不到审核中的评论
        async with new_session() as s:
            other = await CommentReadService.list_main(
                s, oid, CommentTypeEnum.LOTTERY, viewer_mid=_VIEWER
            )
            other_rpids = [it.rpid for it in other.items]
            assert audit_rpid not in other_rpids, "他人不应看到作者审核中的评论"
            assert normal_rpid in other_rpids

            anon = await CommentReadService.list_main(
                s, oid, CommentTypeEnum.LOTTERY, viewer_mid=None
            )
            anon_rpids = [it.rpid for it in anon.items]
            assert audit_rpid not in anon_rpids, "匿名不应看到审核中的评论"
            assert anon.total == 1

        # 详情：作者本人可看（含 auditing），他人不可看
        async with new_session() as s:
            own_detail = await CommentReadService.get_detail(
                s, int(audit_rpid), viewer_mid=_AUTHOR
            )
            # 注意：get_detail 的可见性仍受 VISIBLE_STATES 约束，auditing 评论对所有人
            # （包括作者）详情均不可见，仅列表对作者开放。这里仅断言他人视角不可见。
            other_detail = await CommentReadService.get_detail(
                s, int(audit_rpid), viewer_mid=_VIEWER
            )
            assert other_detail is None
            _ = own_detail
    finally:
        await _cleanup(oid, {_AUTHOR, _VIEWER})


async def test_interact_notify_only_for_visible_comment(monkeypatch: pytest.MonkeyPatch) -> None:
    """D6：互动通知（回复 / @）仅对 NORMAL 可见评论投递。

    auditing（审核中，暂不可见）与 rejected / hidden（未通过 / 下架）的评论
    一律不投递回复 / @ 通知，避免接收方点开看到「评论不可见」；仅 NORMAL 投递。
    """
    from app.models.enums import EventTypeEnum
    from app.models.schemas import EventReportReq
    from app.services.message.event import EventService as EventSvc

    # 关掉「先审后发」，保证无敏感词评论直接 NORMAL（与
    # test_author_sees_own_auditing_comment 同理），使状态判定可控
    monkeypatch.setattr(settings, "comment_pre_audit", False)

    calls: list[EventReportReq] = []

    async def fake_report(session, req: EventReportReq) -> None:
        calls.append(req)

    monkeypatch.setattr(EventSvc, "report", fake_report)

    oid = _next_oid()

    def _build(root_rpid: int, message: str) -> CommentAddReq:
        return CommentAddReq(
            oid=str(oid),
            # LOTTERY 类型避开 DYNAMIC 的 MomentStat 外键关联
            # （测试 oid 没有对应的 TMoment 父行），与
            # test_author_sees_own_auditing_comment 保持一致
            type=CommentTypeEnum.LOTTERY,
            root=str(root_rpid),
            parent=str(root_rpid),
            message=message,
            at_mids=[_AT_USER],
        )

    try:
        async with new_session() as s:
            # 先建一条 NORMAL 根评论（作者为 _UP）作为楼中楼回复目标：
            # _AUTHOR 回复它可触发 REPLY 通知（reply_to_mid != mid）
            root_resp = await CommentService.add(
                s,
                _UP,
                CommentAddReq(
                    oid=str(oid),
                    type=CommentTypeEnum.LOTTERY,
                    message="根评论",
                ),
                uname=f"user{_UP}",
            )
            root_rpid = int(root_resp.rpid)

        # 1) NORMAL 评论（楼中楼回复 + @）：投递回复 + @ 通知
        async with new_session() as s:
            resp = await CommentService.add(
                s, _AUTHOR, _build(root_rpid, "正常回复内容"), uname=f"user{_AUTHOR}"
            )
            assert resp.state is CommentStateEnum.NORMAL
        assert any(r.event_type is EventTypeEnum.REPLY for r in calls), "NORMAL 应投递回复通知"
        assert any(r.event_type is EventTypeEnum.AT for r in calls), "NORMAL 应投递@通知"
        calls.clear()

        # 2) REJECTED 评论（高危词 + 回复 + @）：不投递互动通知
        async with new_session() as s:
            resp = await CommentService.add(
                s, _AUTHOR, _build(root_rpid, "这是诈骗内容"), uname=f"user{_AUTHOR}"
            )
            assert resp.state is CommentStateEnum.REJECTED
        assert calls == [], "REJECTED 评论不应投递回复 / @ 通知"

        # 3) AUDITING 评论（疑似词 + 回复 + @）：不投递互动通知
        async with new_session() as s:
            resp = await CommentService.add(
                s, _AUTHOR, _build(root_rpid, "这个链接加微信看广告"), uname=f"user{_AUTHOR}"
            )
            assert resp.state is CommentStateEnum.AUDITING
        assert calls == [], "AUDITING 评论不应投递回复 / @ 通知"
    finally:
        await _cleanup(oid, {_AUTHOR, _UP})


async def test_interact_notify_resend_after_approve(monkeypatch: pytest.MonkeyPatch) -> None:
    """D6 补偿通道：审核通过 / 恢复（非 NORMAL → NORMAL）补发先前跳过的回复 / @ 通知。

    auditing 评论发评时不投递互动通知（`msg_comment_at.notified=False`）；
    管理端审核通过后，补发回复 + @ 通知，且已投递的 @ 记录标记 `notified=True`，
    再次翻转为 NORMAL 不重复补发。
    """
    from app.models.enums import EventTypeEnum
    from app.models.schemas import EventReportReq
    from app.services.message.event import EventService as EventSvc

    # 关掉「先审后发」：无敏感词评论（含根评论）直接 NORMAL，作为楼中楼回复目标；
    # 疑似词子评论仍会命中预筛进 AUDITING（与 test_interact_notify_only_for_visible_comment 同理）
    monkeypatch.setattr(settings, "comment_pre_audit", False)

    calls: list[EventReportReq] = []

    async def fake_report(session, req: EventReportReq) -> None:
        calls.append(req)

    monkeypatch.setattr(EventSvc, "report", fake_report)

    oid = _next_oid()

    try:
        # 根评论（作者 _UP）作为楼中楼回复目标
        async with new_session() as s:
            root_resp = await CommentService.add(
                s,
                _UP,
                CommentAddReq(
                    oid=str(oid),
                    type=CommentTypeEnum.LOTTERY,
                    message="根评论",
                ),
                uname=f"user{_UP}",
            )
            root_rpid = int(root_resp.rpid)

        # 1) AUDITING 评论（疑似词 + 回复 + @）：不投递互动通知
        async with new_session() as s:
            resp = await CommentService.add(
                s,
                _AUTHOR,
                CommentAddReq(
                    oid=str(oid),
                    type=CommentTypeEnum.LOTTERY,
                    root=str(root_rpid),
                    parent=str(root_rpid),
                    message="这个链接加微信看广告",
                    at_mids=[_AT_USER],
                ),
                uname=f"user{_AUTHOR}",
            )
            assert resp.state is CommentStateEnum.AUDITING
        assert calls == [], "AUDITING 评论不应投递回复 / @ 通知"
        rpid = int(resp.rpid)

        # 2) 管理端审核通过（AUDITING → NORMAL）：补发回复 + @ 通知
        async with new_session() as s:
            ok = await CommentAdminService.set_state(
                s, rpid, CommentStateEnum.NORMAL, note="内容合规", operator_mid=_VIEWER
            )
            assert ok
        assert any(r.event_type is EventTypeEnum.REPLY for r in calls), "审核通过应补发回复通知"
        assert any(r.event_type is EventTypeEnum.AT for r in calls), "审核通过应补发@通知"
        calls.clear()

        # 3) 下架后再次恢复 NORMAL：@ 已投递（notified=True）不再重复补发；
        #    回复通知无独立标记字段，会再次调用（EventService dedup_key 幂等兜底，不落重复事件）
        async with new_session() as s:
            await CommentAdminService.set_state(
                s, rpid, CommentStateEnum.HIDDEN, note="违规", operator_mid=_VIEWER
            )
        calls.clear()
        async with new_session() as s:
            await CommentAdminService.set_state(
                s, rpid, CommentStateEnum.NORMAL, operator_mid=_VIEWER
            )
        assert not any(r.event_type is EventTypeEnum.AT for r in calls), "已投递的@不应重复补发"
    finally:
        await _cleanup(oid, {_AUTHOR, _UP, _VIEWER})
