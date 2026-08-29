"""评论计数口径同步测试（P10-T3，2.13.0）。

覆盖：评论审核状态翻转（通过 / 驳回 / 恢复）时，评论系统冗余计数
`msg_comment_subject.root_count` / `all_count` 与动态统计 `TInteractionStat.commentCount`
保持同步，确保**未审核评论不计入评论数量**（Feed/详情展示的 stat.commentCount 读的
正是 root_count，P10-T1 bugfix）。
"""

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlmodel import col, select, text
from sqlmodel.ext.asyncio.session import AsyncSession as SQLModelAsyncSession

from app.core import database as db_mod
from app.core.config import settings
from app.core.database import new_session
from app.models.db import CommentIndex, CommentSubject
from app.models.db.moment_tbl import TMoment
from app.models.enums import (
    CommentStateEnum,
    CommentTypeEnum,
    MomentAuditStatusEnum,
    MomentTypeEnum,
)
from app.models.schemas import CommentAddReq
from app.services.message.comment import CommentService
from app.services.message.comment_admin import CommentAdminService


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


_OID = 884_000_000_000
_MID = 906_000
_UP = 906_001
_seq = 0


def _next_oid() -> int:
    global _seq
    _seq += 1
    return _OID + _seq


async def _cleanup(oid: int) -> None:
    async with new_session() as s:
        await s.exec(
            text(
                "DELETE FROM msg_comment_action WHERE rpid IN "
                f"(SELECT rpid FROM msg_comment_index WHERE oid = {oid})"
            )
        )
        await s.exec(text(f"DELETE FROM msg_comment_at WHERE oid = {oid}"))
        await s.exec(
            text(
                "DELETE FROM msg_comment_content WHERE rpid IN "
                f"(SELECT rpid FROM msg_comment_index WHERE oid = {oid})"
            )
        )
        await s.exec(text(f"DELETE FROM msg_comment_index WHERE oid = {oid}"))
        await s.exec(text(f"DELETE FROM msg_comment_subject WHERE oid = {oid}"))
        await s.exec(text(f"DELETE FROM TResourceFeed WHERE bizType = 1 AND bizId = {oid}"))
        await s.exec(
            text(f"DELETE FROM TInteractionStat WHERE bizType = 1 AND bizId = {oid}")
        )
        await s.exec(text(f"DELETE FROM TMoment WHERE dynId = {oid}"))
        await s.commit()


async def _create_moment(session, oid: int) -> None:
    """创建最小 WORD 动态记录，作为评论计数回写的宿主。"""
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


async def _add(session, oid: int) -> int:
    """发布一条 DYNAMIC 一级评论（系统预审核可能置 auditing，或直接 normal）。"""
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


async def _subject_counts(session, oid: int) -> tuple[int, int]:
    subj = (
        await session.exec(
            select(CommentSubject).where(
                col(CommentSubject.oid) == oid,
                col(CommentSubject.type) == CommentTypeEnum.DYNAMIC,
            )
        )
    ).one_or_none()
    if subj is None:
        return (0, 0)
    return (subj.root_count, subj.all_count)


async def _stat_comment_count(session, oid: int) -> int:
    # 2.36.0：动态计数统一 TInteractionStat（bizType=1=DYNAMIC）
    row = (
        await session.exec(
            text(f"SELECT commentCount FROM TInteractionStat WHERE bizType = 1 AND bizId = {oid}")
        )
    ).one_or_none()
    return int(row[0]) if row else 0


async def test_comment_count_sync_on_review():
    """未审核评论不计入数量；通过 / 驳回 / 恢复均同步 root_count/all_count/commentCount。"""
    oid = _next_oid()
    try:
        async with new_session() as s:
            await _create_moment(s, oid)
            rpid = await _add(s, oid)
            await s.commit()
            state = (
                await s.exec(select(CommentIndex.state).where(col(CommentIndex.rpid) == rpid))
            ).one()

        # 若预审置 auditing：未审核状态不计入数量（关键断言）
        if state == CommentStateEnum.AUDITING:
            async with new_session() as s:
                root, allc = await _subject_counts(s, oid)
                assert (root, allc) == (0, 0)
                assert await _stat_comment_count(s, oid) == 0
            # 审核通过（auditing → normal）：计入数量
            async with new_session() as s:
                await CommentAdminService.set_state(s, rpid, CommentStateEnum.NORMAL)
            async with new_session() as s:
                root, allc = await _subject_counts(s, oid)
                assert (root, allc) == (1, 1)
                assert await _stat_comment_count(s, oid) == 1
        else:
            # 直接 normal：计入数量
            async with new_session() as s:
                root, allc = await _subject_counts(s, oid)
                assert (root, allc) == (1, 1)
                assert await _stat_comment_count(s, oid) == 1

        # 驳回（normal → rejected）：未审核评论不再计入数量
        async with new_session() as s:
            await CommentAdminService.set_state(s, rpid, CommentStateEnum.REJECTED)
        async with new_session() as s:
            root, allc = await _subject_counts(s, oid)
            assert (root, allc) == (0, 0)
            assert await _stat_comment_count(s, oid) == 0

        # 恢复（rejected → normal）：重新计入
        async with new_session() as s:
            await CommentAdminService.set_state(s, rpid, CommentStateEnum.NORMAL)
        async with new_session() as s:
            root, allc = await _subject_counts(s, oid)
            assert (root, allc) == (1, 1)
            assert await _stat_comment_count(s, oid) == 1

        # 下架（normal → hidden）：同样不计入
        async with new_session() as s:
            await CommentAdminService.set_state(s, rpid, CommentStateEnum.HIDDEN)
        async with new_session() as s:
            root, allc = await _subject_counts(s, oid)
            assert (root, allc) == (0, 0)
            assert await _stat_comment_count(s, oid) == 0
    finally:
        await _cleanup(oid)


async def test_auditing_not_counted():
    """预审核中的评论不计入评论数量（root_count/commentCount 均为 0）。"""
    oid = _next_oid()
    try:
        async with new_session() as s:
            await _create_moment(s, oid)
            rpid = await _add(s, oid)
            await s.commit()
            state = (
                await s.exec(select(CommentIndex.state).where(col(CommentIndex.rpid) == rpid))
            ).one()
        if state != CommentStateEnum.AUDITING:
            return  # 未开启预审时跳过本场景
        async with new_session() as s:
            root, allc = await _subject_counts(s, oid)
            assert (root, allc) == (0, 0)
            assert await _stat_comment_count(s, oid) == 0
    finally:
        await _cleanup(oid)
