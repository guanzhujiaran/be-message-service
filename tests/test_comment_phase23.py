"""Phase 2–5 评论系统进阶功能集成测试。

覆盖：点赞/点踩幂等与计数、楼中楼预览与展开、置顶、@搜索、管理端审核/明文IP/统计、
防刷、敏感词审核。与 test_comment_crud.py 共用同一真实 MySQL，但使用独立 mid/oid 区间。
"""

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlmodel import select, text
from sqlmodel.ext.asyncio.session import AsyncSession as SQLModelAsyncSession

from app.core import database as db_mod
from app.core.config import settings
from app.core.database import new_pptr_session, new_session, test_pptr_connection
from app.models.db import (
    CommentAction,
    CommentAt,
    CommentContent,
    CommentIndex,
    CommentSubject,
)
from app.models.enums import CommentActionEnum, CommentStateEnum, CommentTypeEnum
from app.models.pptr_user import PptrUserInfo, PptrUserDetail
from app.models.schemas import CommentActionReq, CommentAddReq, CommentTopReq
from app.services.comment import CommentService
from app.services.comment_action import CommentActionService
from app.services.comment_read import CommentReadService
from app.services.comment_admin import CommentAdminService
from app.services.comment_audit import audit_text
from app.services.pptr_user import PptrUserService


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
    yield
    await engine.dispose()


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
        await s.commit()


async def _add(session, mid, oid, *, message="测试评论", up_mid=0, root="0", parent="0", at_mids=None, uname=None):
    return (
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
            ps.add(PptrUserInfo(uid=_SEARCH_MID, user_name="搜索目标用户ABC", role="level0"))
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
            # 审核队列包含它
            queue, total = await CommentAdminService.list_audit_queue(s, 1, 20)
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
