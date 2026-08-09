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

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlmodel import select, text
from sqlmodel.ext.asyncio.session import AsyncSession as SQLModelAsyncSession

from app.core import database as db_mod
from app.core.config import settings
from app.core.database import new_session
from app.models.db import (
    CommentAction,
    CommentAt,
    CommentContent,
    CommentIndex,
    CommentSubject,
)
from app.models.enums import CommentTypeEnum
from app.models.schemas import CommentAddReq
from app.services.comment import CommentService
from app.services.comment_read import CommentReadService


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
        await s.commit()


_msg_seq = 0


async def _add_root(session, mid: int, oid: int, *, message: str | None = None, up_mid: int = 0) -> str:
    # 每条评论用唯一文案，避免触发「同用户同内容 10s 内限流」误伤跨测试用例
    global _msg_seq
    _msg_seq += 1
    msg = message if message is not None else f"一级评论-{_msg_seq}"
    resp = await CommentService.add(
        session,
        mid,
        CommentAddReq(oid=str(oid), type=CommentTypeEnum.DYNAMIC, message=msg, up_mid=up_mid or None),
        uname="tester",
        ip_v4="203.0.113.45",
        ip_v6=None,
    )
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
                    type=CommentTypeEnum.DYNAMIC,
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


async def test_list_main_and_detail_and_count() -> None:
    """列表 / 详情 / 计数三件套读取正确。"""
    oid = _next_oid()
    try:
        async with new_session() as s:
            root_rpid = await _add_root(s, _AUTHOR, oid)

        # 列表（未登录视角，viewer_mid=None）
        async with new_session() as s:
            listing = await CommentReadService.list_main(
                s, oid, CommentTypeEnum.DYNAMIC, sort="time", viewer_mid=None
            )
            assert listing.total == 1
            assert listing.all_count == 1
            assert len(listing.items) == 1
            assert listing.items[0].rpid == root_rpid
            # IP 出参打码：v4 应只保留前两段
            assert listing.items[0].ip_v4_masked == "203.0.*.*", listing.items[0].ip_v4_masked

            detail = await CommentReadService.get_detail(s, int(root_rpid), viewer_mid=None)
            assert detail is not None
            assert detail.member is not None and detail.member.uname == "tester"

            count = await CommentReadService.get_count(s, oid, CommentTypeEnum.DYNAMIC)
            assert count.root_count == 1 and count.all_count == 1
    finally:
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
                    s, _AUTHOR, CommentAddReq(oid=str(oid), type=CommentTypeEnum.DYNAMIC, message="")
                )
            with pytest.raises(ValueError):
                await CommentService.add(
                    s,
                    _AUTHOR,
                    CommentAddReq(oid="not-a-number", type=CommentTypeEnum.DYNAMIC, message="x"),
                )
            # 楼中楼但 root 不存在
            with pytest.raises(ValueError):
                await CommentService.add(
                    s,
                    _AUTHOR,
                    CommentAddReq(
                        oid=str(oid), type=CommentTypeEnum.DYNAMIC, root="999999", parent="999999", message="x"
                    ),
                )
    finally:
        await _cleanup(oid, {_AUTHOR})
