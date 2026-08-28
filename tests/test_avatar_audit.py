"""头像更换审核服务单元测试。

覆盖：
- 提交更换：插入 pending 记录；不修改公开头像。
- 重复提交：新提交覆盖旧 pending（旧 pending 置为 rejected）。
- 待审核列表：仅 pending 进入；已处理不进入。
- 审核通过：状态→approved + 写入公开头像 + 发通知。
- 审核驳回：状态→rejected + 保持旧头像 + 发通知 + 记录驳回原因。
- 我的审核状态：返回最近一条。

复用真实 MySQL 主库 + pptr Postgres，独立 mid 区间避免与既有用例冲突。
"""

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlmodel import text
from sqlmodel.ext.asyncio.session import AsyncSession as SQLModelAsyncSession

from app.core import database as db_mod
from app.core.config import settings
from app.core.database import new_pptr_session, new_session
from app.models.db import NotifyMessage, TUserAvatarAudit
from app.models.enums import AvatarAuditStatusEnum, NotifyTargetTypeEnum
from app.models.pptr_user import PptrUserDetail, PptrUserInfo
from app.services.user.avatar_audit import AvatarAuditService

# 独立区间，避免与既有用例冲突
A_MID = 940001
A_MID2 = 940002
ADMIN_MID = 940099

NEW_AVATAR = "https://example.com/new.png"
OLD_AVATAR = "https://example.com/old.png"


@pytest.fixture(autouse=True)
async def _bind_engine_per_test():
    """每个测试重建 engine（含 pptr），并按本模块专属 mid 区间预清理，保证幂等可重复。

    审核通过会写 pptr Postgres 的 TUserDetail.avatar，pptr engine 需随测试重建绑定当前 loop。
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
            text(f"DELETE FROM TUserAvatarAudit WHERE mid IN ({A_MID}, {A_MID2})")
        )
        await s.exec(
            text(f"DELETE FROM msg_notify WHERE target_value IN ('{A_MID}', '{A_MID2}')")
        )
        await s.commit()
    # 预置 pptr 用户主数据，供审核通过写入公开头像（否则 TUserDetail 插入触发 FK 违约）
    await _seed_pptr_user(A_MID)
    await _seed_pptr_user(A_MID2)
    yield
    async with new_session() as s:
        await s.exec(
            text(f"DELETE FROM TUserAvatarAudit WHERE mid IN ({A_MID}, {A_MID2})")
        )
        await s.exec(
            text(f"DELETE FROM msg_notify WHERE target_value IN ('{A_MID}', '{A_MID2}')")
        )
        await s.commit()
    # 清理 pptr 种子数据
    await _cleanup_pptr_user(A_MID)
    await _cleanup_pptr_user(A_MID2)
    await engine.dispose()
    await pptr_engine.dispose()
    db_mod_pptr.pptr_engine = orig_pptr_engine
    db_mod_pptr.pptr_session_maker = orig_pptr_session_maker


async def _seed_pptr_user(mid: int) -> None:
    """在 pptr Postgres 预置用户主数据（TUserInfo + TUserDetail，初始头像 OLD_AVATAR）。

    审核通过会写 `TUserDetail.avatar`，而 `TUserDetail.mid` 外键强约束父表
    `TUserInfo.uid`：必须先建 TUserInfo，否则 approve 落入 ForeignKeyViolationError。
    无 relationship 声明，UoW 无法推断依赖顺序，须先 flush 父表再写子表
    （对齐 `pptr_user.create_user` / `test_comment_crud` 的种子写法）。
    """
    async with new_pptr_session() as s:
        await s.exec(
            text('DELETE FROM "TUserDetail" WHERE mid = :m'), params={"m": mid}
        )
        await s.exec(
            text('DELETE FROM "TUserInfo" WHERE uid = :m'), params={"m": mid}
        )
        s.add(
            PptrUserInfo(
                uid=mid, user_name=f"avatar_audit_{mid}", role="level0"
            )
        )
        await s.flush()
        s.add(
            PptrUserDetail(
                mid=mid,
                uname=f"avatar_audit_{mid}",
                avatar=OLD_AVATAR,
                sign="",
                sex="保密",
            )
        )
        await s.commit()


async def _cleanup_pptr_user(mid: int) -> None:
    """清理 pptr 种子数据（硬删，避免污染其它用例）。"""
    async with new_pptr_session() as s:
        await s.exec(
            text('DELETE FROM "TUserDetail" WHERE mid = :m'), params={"m": mid}
        )
        await s.exec(
            text('DELETE FROM "TUserInfo" WHERE uid = :m'), params={"m": mid}
        )
        await s.commit()


async def _latest_for(s, mid: int) -> TUserAvatarAudit | None:
    from sqlmodel import select

    return (
        await s.exec(
            select(TUserAvatarAudit)
            .where(TUserAvatarAudit.mid == mid)
            .order_by(TUserAvatarAudit.pk.desc())
            .limit(1)
        )
    ).first()


# ==================== 提交更换 ====================


async def test_submit_creates_pending():
    async with new_session() as s:
        pk = await AvatarAuditService.submit(
            s, uid=A_MID, new_avatar=NEW_AVATAR, old_avatar=OLD_AVATAR
        )
        assert pk > 0
        row = await s.get(TUserAvatarAudit, pk)
        assert row is not None
        assert row.auditStatus is AvatarAuditStatusEnum.PENDING
        assert row.newAvatar == NEW_AVATAR
        assert row.oldAvatar == OLD_AVATAR


async def test_resubmit_overrides_old_pending():
    async with new_session() as s:
        pk1 = await AvatarAuditService.submit(
            s, uid=A_MID, new_avatar=NEW_AVATAR, old_avatar=OLD_AVATAR
        )
        pk2 = await AvatarAuditService.submit(
            s, uid=A_MID, new_avatar="https://example.com/new2.png", old_avatar=OLD_AVATAR
        )
        # 旧 pending 被覆盖为 rejected
        old = await s.get(TUserAvatarAudit, pk1)
        assert old.auditStatus is AvatarAuditStatusEnum.REJECTED
        assert old.auditReason == "已重新提交新申请"
        # 新记录为 pending
        new = await s.get(TUserAvatarAudit, pk2)
        assert new.auditStatus is AvatarAuditStatusEnum.PENDING


# ==================== 待审核列表 ====================


async def test_pending_list_only_pending():
    async with new_session() as s:
        await AvatarAuditService.submit(
            s, uid=A_MID, new_avatar=NEW_AVATAR, old_avatar=OLD_AVATAR
        )
        await AvatarAuditService.submit(
            s, uid=A_MID2, new_avatar=NEW_AVATAR, old_avatar=OLD_AVATAR
        )
        # 处理 A_MID2 的记录为 rejected
        row = await _latest_for(s, A_MID2)
        row.auditStatus = AvatarAuditStatusEnum.REJECTED
        await s.commit()

        resp = await AvatarAuditService.pending_list(s, page_num=1, page_size=20)
        assert resp.total >= 1
        pks = {it.pk for it in resp.items}
        assert (await _latest_for(s, A_MID)).pk in pks  # A_MID 仍 pending
        assert row.pk not in pks  # A_MID2 已 rejected，不应进入


# ==================== 审核通过 ====================


async def test_approve_sets_approved():
    async with new_session() as s:
        pk = await AvatarAuditService.submit(
            s, uid=A_MID, new_avatar=NEW_AVATAR, old_avatar=OLD_AVATAR
        )
        item = await AvatarAuditService.approve(
            s, pk, operator_mid=ADMIN_MID, remark="ok"
        )
        assert item.auditStatus == AvatarAuditStatusEnum.APPROVED.value
        row = await s.get(TUserAvatarAudit, pk)
        assert row.auditStatus is AvatarAuditStatusEnum.APPROVED
        assert row.auditOperatorMid == ADMIN_MID
    # 公开头像（pptr TUserDetail.avatar）应已写入新头像（FK 外键需父表 TUserInfo 存在）
    from sqlmodel import select

    async with new_pptr_session() as s:
        detail = (
            await s.exec(select(PptrUserDetail).where(PptrUserDetail.mid == A_MID))
        ).first()
        assert detail is not None
        assert detail.avatar == NEW_AVATAR


async def test_approve_notifies_user():
    async with new_session() as s:
        pk = await AvatarAuditService.submit(
            s, uid=A_MID, new_avatar=NEW_AVATAR, old_avatar=OLD_AVATAR
        )
        await AvatarAuditService.approve(s, pk, operator_mid=ADMIN_MID)
    from sqlmodel import select

    async with new_session() as s2:
        rows = (
            await s2.exec(
                select(NotifyMessage).where(
                    NotifyMessage.target_type == NotifyTargetTypeEnum.CUSTOM,
                    NotifyMessage.target_value == str(A_MID),
                )
            )
        ).all()
        assert any("头像审核通过" in (r.title or "") for r in rows)


async def test_approve_twice_raises():
    async with new_session() as s:
        pk = await AvatarAuditService.submit(
            s, uid=A_MID, new_avatar=NEW_AVATAR, old_avatar=OLD_AVATAR
        )
        await AvatarAuditService.approve(s, pk, operator_mid=ADMIN_MID)
        import pytest as _pytest

        with _pytest.raises(ValueError):
            await AvatarAuditService.approve(s, pk, operator_mid=ADMIN_MID)


# ==================== 审核驳回 ====================


async def test_reject_sets_rejected_and_reason():
    async with new_session() as s:
        pk = await AvatarAuditService.submit(
            s, uid=A_MID, new_avatar=NEW_AVATAR, old_avatar=OLD_AVATAR
        )
        item = await AvatarAuditService.reject(
            s, pk, operator_mid=ADMIN_MID, reason="图片不清晰"
        )
        assert item.auditStatus == AvatarAuditStatusEnum.REJECTED.value
        row = await s.get(TUserAvatarAudit, pk)
        assert row.auditStatus is AvatarAuditStatusEnum.REJECTED
        assert row.auditReason == "图片不清晰"
        assert row.auditOperatorMid == ADMIN_MID


async def test_reject_notifies_user():
    async with new_session() as s:
        pk = await AvatarAuditService.submit(
            s, uid=A_MID, new_avatar=NEW_AVATAR, old_avatar=OLD_AVATAR
        )
        await AvatarAuditService.reject(s, pk, operator_mid=ADMIN_MID, reason="违规")
    from sqlmodel import select

    async with new_session() as s2:
        rows = (
            await s2.exec(
                select(NotifyMessage).where(
                    NotifyMessage.target_type == NotifyTargetTypeEnum.CUSTOM,
                    NotifyMessage.target_value == str(A_MID),
                )
            )
        ).all()
        assert any("头像审核未通过" in (r.title or "") for r in rows)


# ==================== 我的审核状态 ====================


async def test_mine_returns_latest():
    async with new_session() as s:
        await AvatarAuditService.submit(
            s, uid=A_MID, new_avatar=NEW_AVATAR, old_avatar=OLD_AVATAR
        )
        mine = await AvatarAuditService.mine(s, uid=A_MID)
        assert mine is not None
        assert mine.auditStatus == AvatarAuditStatusEnum.PENDING.value
        assert mine.newAvatar == NEW_AVATAR


async def test_mine_none_when_no_record():
    async with new_session() as s:
        mine = await AvatarAuditService.mine(s, uid=A_MID)
        assert mine is None


__all__ = [
    "test_approve_notifies_user",
    "test_approve_sets_approved",
    "test_approve_twice_raises",
    "test_mine_none_when_no_record",
    "test_mine_returns_latest",
    "test_pending_list_only_pending",
    "test_reject_notifies_user",
    "test_reject_sets_rejected_and_reason",
    "test_resubmit_overrides_old_pending",
    "test_submit_creates_pending",
]
