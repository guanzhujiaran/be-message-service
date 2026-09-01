"""Phase 1 — 评论系统基础读写集成测试。

直接驱动 `CommentService` / `CommentReadService`，对真实 MySQL 验证 Phase 1 的关键语义：

- 发表一级评论：同事务落索引 + 正文 + 原子累加评论区计数。
- 发表楼中楼：树形关系（root/parent/dialog/reply_to_mid）推导正确，rcount +1。
- 列表：一级评论数（root_count）与全量计数（all_count）读冗余列，sql 次数恒定。
- 详情 / 计数：软删后详情返回 None、计数随之回落。
- 删除权限：作者 / UP / 管理员可删，越权返回 affected=0。
- 入参校验：空正文、非法 oid 等应抛 ValueError。

IP 只存原始地址、枚举落 VARCHAR 等约束在 test_phase_e_enums.py 中回归。
"""

from datetime import datetime

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlmodel import col, select, text
from sqlmodel.ext.asyncio.session import AsyncSession as SQLModelAsyncSession

from app.core import database as db_mod
from app.core.config import settings
from app.core.database import new_pptr_session, new_session
from app.exceptions import CommentNotInteractiveException
from app.models.db import (
    CommentAt,
    CommentIndex,
    CommentSubject,
    TMoment,
    )
from app.models.pptr_user import PptrUserDetail, PptrUserInfo
from app.models.enums import (
    CommentStateEnum,
    InteractionBizTypeEnum,
    MomentAuditStatusEnum,
    MomentTypeEnum,
    )
from app.models.schemas import CommentAddReq
from app.services.comment import CommentService
from app.services.comment.comment_read import CommentReadService


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


# 独立 oid / mid 区间，避免与其他 Phase 用例互相干扰
_OID_BASE = 882_000_000_000
_AUTHOR = 901_000
_UP = 901_001
_STRANGER = 901_002
_AT_USER = 901_003
_seq = 0


def _next_oid() -> int:
    global _seq
    _seq += 1
    return _OID_BASE + _seq


async def _cleanup(oid: int, mids: set[int]) -> None:
    async with new_session() as s:
        await s.exec(text(f"DELETE FROM msg_comment_action WHERE rpid IN (SELECT rpid FROM msg_comment_index WHERE oid = {oid})"))
        await s.exec(text(f"DELETE FROM msg_comment_at WHERE oid = {oid}"))
        await s.exec(text(f"DELETE FROM msg_comment_content WHERE rpid IN (SELECT rpid FROM msg_comment_index WHERE oid = {oid})"))
        await s.exec(text(f"DELETE FROM msg_comment_index WHERE oid = {oid}"))
        await s.exec(text(f"DELETE FROM msg_comment_subject WHERE oid = {oid}"))
        # 测试挂载的真实动态行（子表由 FK ON DELETE CASCADE 级联清理）
        await s.exec(text(f"DELETE FROM TMoment WHERE dynId = {oid}"))
        await s.commit()


_msg_seq = 0


async def _ensure_moment(session, oid: int, mid: int) -> None:
    """确保测试 oid 有对应真实 TMoment 行（DYNAMIC 评论计数回写的宿主）。

    DYNAMIC 评论的 `commentCount ±1` 经 ``MomentStatService`` 回写动态计数
    （2.36.0 起为 `TInteractionStat`），评论测试需把 oid 挂到真实动态上以
    完整覆盖 DYNAMIC 生命周期。
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

    `comment_pre_audit=True` 下 `CommentService.add` 发布即 `auditing`，而
    auditing 不计入 `root_count/all_count`（见 comment.py「仅 NORMAL 才计入」），
    审核通过时须补 +1（与 `CommentAdminService.set_state(NORMAL)` 的计数语义一致）。

    不走 `set_state`：其内部会回写动态计数（经 ``MomentStatService``），
    测试 oid 为虚构值、无真实 `TMoment` 宿主行。
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
                col(CommentSubject.type) == InteractionBizTypeEnum.DYNAMIC,
            )
        )
    ).one_or_none()
    if subject is not None:
        subject.all_count += 1
        if is_root:
            subject.root_count += 1
        session.add(subject)
    await session.commit()


async def _add_root(session, mid: int, oid: int, *, message: str | None = None, up_mid: int = 0) -> str:
    # 每条评论用唯一文案，避免触发「同用户同内容 10s 内限流」误伤跨测试用例
    global _msg_seq
    _msg_seq += 1
    msg = message if message is not None else f"一级评论-{_msg_seq}"
    resp = await CommentService.add(
        session,
        mid,
        CommentAddReq(oid=str(oid), type=InteractionBizTypeEnum.DYNAMIC, message=msg, up_mid=up_mid or None),
        uname="tester",
        ip_v4="203.0.113.45",
        ip_v6=None,
    )
    # 评论挂在真实动态上（stat 回写的父行）
    await _ensure_moment(session, oid, mid)
    # 先审后发：发布即 auditing，显式审核通过置 NORMAL（一级评论补 root_count）
    await _pass_audit(session, int(resp.rpid), oid, is_root=True)
    return resp.rpid


async def test_publish_root_and_sub_comment() -> None:
    """发表一级评论 + 楼中楼，校验树形关系与计数原子累加。"""
    oid = _next_oid()
    try:
        async with new_session() as s:
            root_rpid = await _add_root(s, _AUTHOR, oid)

            # 楼中楼：直接回复一级评论（parent==root），应开启一条新会话串
            sub_resp = await CommentService.add(
                s,
                _STRANGER,
                CommentAddReq(
                    oid=str(oid),
                    type=InteractionBizTypeEnum.DYNAMIC,
                    root=root_rpid,
                    parent=root_rpid,
                    message="回复楼主",
                    at_mids=[_AT_USER],
                ),
                uname="replier",
                ip_v4=None,
                ip_v6="2408:8207:78d2:1a00::1",
            )
            assert int(sub_resp.rpid) > 0
            # 先审后发：楼中楼同样 auditing，显式审核通过后计入 all_count
            await _pass_audit(s, int(sub_resp.rpid), oid, is_root=False)

        # 跨会话读出，验证计数与关系
        async with new_session() as s:
            sub = (
                await s.exec(select(CommentIndex).where(CommentIndex.rpid == int(sub_resp.rpid)))
            ).one()
            assert sub.root == int(root_rpid), "楼中楼 root 应指向一级评论"
            assert sub.parent == int(root_rpid)
            assert sub.dialog == int(root_rpid), "直接回复一级评论 dialog 等于 root"
            assert sub.reply_to_mid == _AUTHOR

            subject = (
                await s.exec(select(CommentSubject).where(CommentSubject.oid == oid))
            ).one()
            assert subject.root_count == 1, "一级评论计数应为 1"
            assert subject.all_count == 2, "含楼中楼总数应为 2"

            # @关系落表
            at_rows = (
                await s.exec(select(CommentAt).where(CommentAt.rpid == int(sub_resp.rpid)))
            ).all()
            assert [a.at_mid for a in at_rows] == [_AT_USER]
    finally:
        await _cleanup(oid, {_AUTHOR, _STRANGER, _AT_USER})


async def test_reply_to_sub_comment_stays_under_root() -> None:
    """楼中楼之间的回复（三级）只挂在一级评论下，不归二级评论管。

    校验：回复二级评论产生的三级评论，其 root 与 parent 都指向一级评论，
    reply_to_mid 指向被回复的二级作者。这样删除二级评论时，
    三级评论作为一级评论的子回复仍可被正常读出，不会被一并删除。
    """
    oid = _next_oid()
    try:
        async with new_session() as s:
            root_rpid = await _add_root(s, _AUTHOR, oid)

            # 二级评论：直接回复一级评论
            sub2_resp = await CommentService.add(
                s,
                _STRANGER,
                CommentAddReq(
                    oid=str(oid),
                    type=InteractionBizTypeEnum.DYNAMIC,
                    root=root_rpid,
                    parent=root_rpid,
                    message="二级评论",
                ),
                uname="sub2",
                ip_v4=None,
                ip_v6="2408:8207:78d2:1a00::1",
            )
            await _pass_audit(s, int(sub2_resp.rpid), oid, is_root=False)

            # 三级评论：回复二级评论（parent 传二级 rpid）
            sub3_resp = await CommentService.add(
                s,
                _AT_USER,
                CommentAddReq(
                    oid=str(oid),
                    type=InteractionBizTypeEnum.DYNAMIC,
                    root=root_rpid,
                    parent=sub2_resp.rpid,
                    message="回复二级评论",
                ),
                uname="sub3",
                ip_v4=None,
                ip_v6="2408:8207:78d2:1a00::1",
            )
            await _pass_audit(s, int(sub3_resp.rpid), oid, is_root=False)

        # 读出校验层级关系
        async with new_session() as s:
            sub3 = (
                await s.exec(select(CommentIndex).where(CommentIndex.rpid == int(sub3_resp.rpid)))
            ).one()
            assert sub3.root == int(root_rpid), "三级评论 root 应指向一级评论"
            assert sub3.parent == int(root_rpid), "三级评论 parent 应指向一级评论（不归二级管）"
            assert sub3.reply_to_mid == _STRANGER, "三级评论应显示回复 @二级作者"

            root_row = (
                await s.exec(select(CommentIndex).where(CommentIndex.rpid == int(root_rpid)))
            ).one()
            assert root_row.rcount == 2, "一级评论的楼中楼计数应为 2（二级 + 三级）"

            # 删除二级评论后，三级评论仍应存在于一级评论的楼中楼中
            await CommentService.delete(s, _STRANGER, int(sub2_resp.rpid))
            subs = (
                await s.exec(
                    select(CommentIndex).where(
                        CommentIndex.root == int(root_rpid),
                        CommentIndex.state.in_([CommentStateEnum.NORMAL]),
                    )
                )
            ).all()
            alive_rpids = {int(r.rpid) for r in subs}
            assert int(sub3_resp.rpid) in alive_rpids, "删除二级评论后，三级评论不应被删除"
    finally:
        await _cleanup(oid, {_AUTHOR, _STRANGER, _AT_USER})


async def test_list_main_and_detail_and_count() -> None:
    """列表 / 详情 / 计数三件套读取正确。"""
    oid = _next_oid()
    try:
        # detail.member 回查 pptr Postgres：测试库缺该用户则 member=None，
        # 与 phase23 test_at_search 一致地 seed 用户资料（uname 与 _add_root 对齐）
        async with new_pptr_session() as ps:
            await ps.exec(
                text('DELETE FROM "TUserDetail" WHERE mid = :m'),
                params={"m": _AUTHOR},
            )
            await ps.exec(
                text('DELETE FROM "TUserInfo" WHERE uid = :m'),
                params={"m": _AUTHOR},
            )
            # 无 relationship 声明，UoW 无法推断依赖顺序：必须先 flush 父表
            # TUserInfo 让 mid 在当前事务可见，再写 TUserDetail（见 pptr_user.create_user）
            ps.add(PptrUserInfo(uid=_AUTHOR, user_name="tester", role="level0"))
            await ps.flush()
            ps.add(PptrUserDetail(mid=_AUTHOR, uname="tester", sign="", sex=""))
            await ps.commit()

        async with new_session() as s:
            root_rpid = await _add_root(s, _AUTHOR, oid)

        # 列表（未登录视角，viewer_mid=None）
        async with new_session() as s:
            listing = await CommentReadService.list_main(
                s, oid, InteractionBizTypeEnum.DYNAMIC, sort="time", viewer_mid=None
            )
            assert listing.total == 1
            assert listing.all_count == 1
            assert len(listing.items) == 1
            assert listing.items[0].rpid == root_rpid
            # 2.42.0：出参不返回明文 IP（CommentItem 无 ip_v4 字段，仅属地 ip_location/ip_isp）

            detail = await CommentReadService.get_detail(s, int(root_rpid), viewer_mid=None)
            assert detail is not None
            assert detail.member is not None and detail.member.uname == "tester"

            count = await CommentReadService.get_count(s, oid, InteractionBizTypeEnum.DYNAMIC)
            assert count.root_count == 1 and count.all_count == 1
    finally:
        # 清理 pptr seed 用户（硬删，避免污染其它用例 / 模块）
        try:
            async with new_pptr_session() as ps:
                await ps.exec(
                    text('DELETE FROM "TUserDetail" WHERE mid = :m'),
                    params={"m": _AUTHOR},
                )
                await ps.exec(
                    text('DELETE FROM "TUserInfo" WHERE uid = :m'),
                    params={"m": _AUTHOR},
                )
                await ps.commit()
        except Exception:  # noqa: BLE001
            pass
        await _cleanup(oid, {_AUTHOR})


async def test_delete_by_author_then_hidden() -> None:
    """作者删除后详情不可见、计数回落。"""
    oid = _next_oid()
    try:
        async with new_session() as s:
            root_rpid = await _add_root(s, _AUTHOR, oid)

        async with new_session() as s:
            affected, msg = await CommentService.delete(s, _AUTHOR, int(root_rpid))
            assert affected == 1, msg

            # 软删后详情返回 None（VISIBLE_STATES 仅 normal）
            detail = await CommentReadService.get_detail(s, int(root_rpid))
            assert detail is None, "已删除评论详情应不可见"

            subject = (
                await s.exec(select(CommentSubject).where(CommentSubject.oid == oid))
            ).one()
            assert subject.root_count == 0, "删除后一级计数应回落"
            assert subject.all_count == 0

        # 越权删除：陌生人删除他人（未删的）评论应被拒
        async with new_session() as s:
            other_rpid = await _add_root(s, _AUTHOR, oid, message="另一条")
            affected2, msg2 = await CommentService.delete(s, _STRANGER, int(other_rpid))
            assert affected2 == 0, "陌生人不应能删除他人评论"
            assert "无权" in msg2
    finally:
        await _cleanup(oid, {_AUTHOR, _STRANGER})


async def test_delete_by_up_and_admin() -> None:
    """UP 主与管理员均可删除评论。"""
    oid = _next_oid()
    oid2 = _next_oid()
    try:
        async with new_session() as s:
            # up_mid 指向 _UP，作者为 _AUTHOR
            root_rpid = await _add_root(s, _AUTHOR, oid, up_mid=_UP)

        # UP 主删除
        async with new_session() as s:
            affected, _ = await CommentService.delete(s, _UP, int(root_rpid))
            assert affected == 1, "UP 主应能删除评论"
        # 重新发一条给管理员删
        async with new_session() as s:
            root_rpid2 = await _add_root(s, _AUTHOR, oid2)
            affected, _ = await CommentService.delete(s, _STRANGER, int(root_rpid2), is_admin=True)
            assert affected == 1, "管理员应能删除评论"
    finally:
        await _cleanup(oid, {_AUTHOR, _UP, _STRANGER})
        await _cleanup(oid2, {_AUTHOR, _STRANGER})


async def test_input_validation() -> None:
    """入参非法应抛 ValueError。"""
    oid = _next_oid()
    try:
        async with new_session() as s:
            with pytest.raises(ValueError):
                await CommentService.add(
                    s, _AUTHOR, CommentAddReq(oid=str(oid), type=InteractionBizTypeEnum.DYNAMIC, message="")
                )
            with pytest.raises(ValueError):
                await CommentService.add(
                    s,
                    _AUTHOR,
                    CommentAddReq(oid="not-a-number", type=InteractionBizTypeEnum.DYNAMIC, message="x"),
                )
            # 楼中楼但 root 不存在 → 按状态给出准确反馈的异常
            with pytest.raises(CommentNotInteractiveException):
                await CommentService.add(
                    s,
                    _AUTHOR,
                    CommentAddReq(
                        oid=str(oid), type=InteractionBizTypeEnum.DYNAMIC, root="999999", parent="999999", message="x"
                    ),
                )
    finally:
        await _cleanup(oid, {_AUTHOR})
