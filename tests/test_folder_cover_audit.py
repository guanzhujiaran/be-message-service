"""收藏夹封面审核服务单元测试（P24-T5，2.28.0）。

覆盖：
- 创建收藏夹携带封面：封面不落库（cover_url=None）+ 插入 pending 审核记录，返回 coverAuditStatus=pending。
- 更新收藏夹封面：进入 pending，不直接改 cover_url。
- 重复提交：同夹旧 pending 覆盖为 rejected。
- 封面校验失败：`verify_avatar_url` 失败抛 ValueError（路由层 422）。
- 空串清除封面：直接清 cover_url，不经审核。
- 审核通过：状态→approved + 写入 cover_url + 发通知。
- 审核驳回：状态→rejected + 保持原封面 + 发通知 + 记录驳回原因。
- 待审核列表：仅 pending 进入。
- 我的审核状态：返回该夹最近一条。

复用真实 MySQL 主库 + pptr Postgres，独立 mid/folderId 区间避免与既有用例冲突。
封面校验网络调用在测试中经 monkeypatch 替换（成功 / 失败两态）。
"""

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlmodel import select, text
from sqlmodel.ext.asyncio.session import AsyncSession as SQLModelAsyncSession

from app.core import database as db_mod
from app.core.config import settings
from app.core.database import new_session
from app.core.sharding import generate_moment_id
from app.models.db import NotifyMessage, TFavoriteFolder, TFolderCoverAudit
from app.models.enums import FolderCoverAuditStatusEnum, NotifyTargetTypeEnum
from app.services.user.folder_cover_audit import FolderCoverAuditService
from app.services.interaction_actions.folder import FavoriteFolderAction

# 独立区间，避免与既有用例冲突
C_MID = 941001
C_MID2 = 941002
ADMIN_MID = 941099

NEW_COVER = "https://example.com/new.png"
OLD_COVER = "https://example.com/old.png"


@pytest.fixture(autouse=True)
async def _bind_engine_per_test():
    """每个测试重建 engine（含 pptr），并按本模块专属 mid 区间预清理，保证幂等可重复。"""
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
            text(f"DELETE FROM TFolderCoverAudit WHERE mid IN ({C_MID}, {C_MID2})")
        )
        await s.exec(
            text(f"DELETE FROM TFavoriteFolder WHERE mid IN ({C_MID}, {C_MID2})")
        )
        await s.exec(
            text(f"DELETE FROM msg_notify WHERE target_value IN ('{C_MID}', '{C_MID2}')")
        )
        await s.commit()
    yield
    async with new_session() as s:
        await s.exec(
            text(f"DELETE FROM TFolderCoverAudit WHERE mid IN ({C_MID}, {C_MID2})")
        )
        await s.exec(
            text(f"DELETE FROM TFavoriteFolder WHERE mid IN ({C_MID}, {C_MID2})")
        )
        await s.exec(
            text(f"DELETE FROM msg_notify WHERE target_value IN ('{C_MID}', '{C_MID2}')")
        )
        await s.commit()
    await engine.dispose()
    await pptr_engine.dispose()
    db_mod_pptr.pptr_engine = orig_pptr_engine
    db_mod_pptr.pptr_session_maker = orig_pptr_session_maker


def _ok_verify(monkeypatch):
    """把封面校验替换为直接通过。"""

    async def _fake(url, *, transport=None, label="头像"):
        return True, ""

    monkeypatch.setattr("app.services.interaction_actions.folder.verify_avatar_url", _fake)


def _fail_verify(monkeypatch):
    """把封面校验替换为失败。"""

    async def _fake(url, *, transport=None, label="头像"):
        return False, f"{label}图片不能超过 1MB"

    monkeypatch.setattr("app.services.interaction_actions.folder.verify_avatar_url", _fake)


async def _create_folder(s, mid: int, cover_url: str | None = None):
    """便捷：创建收藏夹并返回 folder_id。"""
    folder_id, status = await FavoriteFolderAction(s, mid).create(
        name="测试收藏夹", description=None, cover_url=cover_url
    )
    return folder_id, status


async def _latest_for_folder(s, folder_id: int) -> TFolderCoverAudit | None:
    return (
        await s.exec(
            select(TFolderCoverAudit)
            .where(TFolderCoverAudit.folderId == folder_id)
            .order_by(TFolderCoverAudit.pk.desc())
            .limit(1)
        )
    ).first()


# ==================== 创建收藏夹携带封面 ====================


async def test_create_folder_with_cover_goes_pending(monkeypatch):
    _ok_verify(monkeypatch)
    async with new_session() as s:
        folder_id, status = await _create_folder(s, C_MID, cover_url=NEW_COVER)
        assert status == FolderCoverAuditStatusEnum.PENDING
        # 封面不落库（先审后发）
        folder = await s.get(TFavoriteFolder, folder_id)
        assert folder.cover_url is None
        # 存在 pending 审核记录
        audit = await _latest_for_folder(s, folder_id)
        assert audit is not None
        assert audit.auditStatus is FolderCoverAuditStatusEnum.PENDING
        assert audit.newCover == NEW_COVER
        assert audit.oldCover is None


async def test_create_folder_without_cover_no_audit(monkeypatch):
    _ok_verify(monkeypatch)
    async with new_session() as s:
        folder_id, status = await _create_folder(s, C_MID)
        assert status is None
        assert await _latest_for_folder(s, folder_id) is None


async def test_create_folder_cover_verify_failure(monkeypatch):
    _fail_verify(monkeypatch)
    async with new_session() as s:
        with pytest.raises(ValueError) as ei:
            await _create_folder(s, C_MID, cover_url="https://example.com/big.png")
        assert "1MB" in str(ei.value)
        # 校验失败不应残留收藏夹
        rows = (
            await s.exec(
                select(TFavoriteFolder).where(TFavoriteFolder.mid == C_MID)
            )
        ).all()
        assert rows == []


# ==================== 更新收藏夹封面 ====================


async def test_update_folder_cover_goes_pending(monkeypatch):
    _ok_verify(monkeypatch)
    async with new_session() as s:
        folder_id, _ = await _create_folder(s, C_MID)
        status = await FavoriteFolderAction(s, C_MID).update(
            folder_id, cover_url=NEW_COVER
        )
        assert status == FolderCoverAuditStatusEnum.PENDING
        folder = await s.get(TFavoriteFolder, folder_id)
        assert folder.cover_url is None  # 封面未直接生效
        audit = await _latest_for_folder(s, folder_id)
        assert audit is not None
        assert audit.auditStatus is FolderCoverAuditStatusEnum.PENDING
        assert audit.newCover == NEW_COVER


async def test_update_folder_cover_verify_failure(monkeypatch):
    _fail_verify(monkeypatch)
    async with new_session() as s:
        folder_id, _ = await _create_folder(s, C_MID)
        with pytest.raises(ValueError) as ei:
            await FavoriteFolderAction(s, C_MID).update(
                folder_id, cover_url="https://example.com/big.png"
            )
        assert "1MB" in str(ei.value)


async def test_update_folder_clear_cover(monkeypatch):
    _ok_verify(monkeypatch)
    async with new_session() as s:
        folder_id, _ = await _create_folder(s, C_MID)
        # 先审核通过一个封面
        await FavoriteFolderAction(s, C_MID).update(folder_id, cover_url=OLD_COVER)
        audit = await _latest_for_folder(s, folder_id)
        await FolderCoverAuditService.approve(s, audit.pk, operator_mid=ADMIN_MID)
        folder = await s.get(TFavoriteFolder, folder_id)
        assert folder.cover_url == OLD_COVER
        # 空串清除封面：直接清 cover_url，不经审核
        status = await FavoriteFolderAction(s, C_MID).update(folder_id, cover_url="")
        assert status is None
        folder = await s.get(TFavoriteFolder, folder_id)
        assert folder.cover_url is None
        # 未产生新审核记录
        latest = await _latest_for_folder(s, folder_id)
        assert latest.auditStatus is FolderCoverAuditStatusEnum.APPROVED


# ==================== 重复提交覆盖 ====================


async def test_resubmit_overrides_old_pending(monkeypatch):
    _ok_verify(monkeypatch)
    async with new_session() as s:
        folder_id, _ = await _create_folder(s, C_MID)
        await FavoriteFolderAction(s, C_MID).update(folder_id, cover_url=NEW_COVER)
        await FavoriteFolderAction(s, C_MID).update(
            folder_id, cover_url="https://example.com/new2.png"
        )
        rows = (
            await s.exec(
                select(TFolderCoverAudit).where(TFolderCoverAudit.folderId == folder_id)
            )
        ).all()
        assert len(rows) == 2
        pending = [r for r in rows if r.auditStatus is FolderCoverAuditStatusEnum.PENDING]
        rejected = [r for r in rows if r.auditStatus is FolderCoverAuditStatusEnum.REJECTED]
        assert len(pending) == 1
        assert len(rejected) == 1
        assert rejected[0].auditReason == "已重新提交新申请"


# ==================== 待审核列表 ====================


async def test_pending_list_only_pending(monkeypatch):
    _ok_verify(monkeypatch)
    async with new_session() as s:
        folder_id1, _ = await _create_folder(s, C_MID, cover_url=NEW_COVER)
        folder_id2, _ = await _create_folder(s, C_MID2, cover_url=NEW_COVER)
        # 处理 C_MID2 的记录为 rejected
        audit2 = await _latest_for_folder(s, folder_id2)
        audit2.auditStatus = FolderCoverAuditStatusEnum.REJECTED
        await s.commit()

        resp = await FolderCoverAuditService.pending_list(s, page_num=1, page_size=20)
        pks = {it.pk for it in resp.items}
        assert (await _latest_for_folder(s, folder_id1)).pk in pks  # C_MID 仍 pending
        assert audit2.pk not in pks  # C_MID2 已 rejected，不应进入


# ==================== 审核通过 ====================


async def test_approve_writes_cover_and_notifies(monkeypatch):
    _ok_verify(monkeypatch)
    async with new_session() as s:
        folder_id, _ = await _create_folder(s, C_MID, cover_url=NEW_COVER)
        audit = await _latest_for_folder(s, folder_id)
        item = await FolderCoverAuditService.approve(
            s, audit.pk, operator_mid=ADMIN_MID, remark="ok"
        )
        assert item.auditStatus == FolderCoverAuditStatusEnum.APPROVED.value
        # 封面写入公开收藏夹
        folder = await s.get(TFavoriteFolder, folder_id)
        assert folder.cover_url == NEW_COVER
        row = await s.get(TFolderCoverAudit, audit.pk)
        assert row.auditStatus is FolderCoverAuditStatusEnum.APPROVED
        assert row.auditOperatorMid == ADMIN_MID

    async with new_session() as s2:
        rows = (
            await s2.exec(
                select(NotifyMessage).where(
                    NotifyMessage.target_type == NotifyTargetTypeEnum.CUSTOM,
                    NotifyMessage.target_value == str(C_MID),
                )
            )
        ).all()
        assert any("收藏夹封面审核通过" in (r.title or "") for r in rows)


async def test_approve_twice_raises(monkeypatch):
    _ok_verify(monkeypatch)
    async with new_session() as s:
        folder_id, _ = await _create_folder(s, C_MID, cover_url=NEW_COVER)
        audit = await _latest_for_folder(s, folder_id)
        await FolderCoverAuditService.approve(s, audit.pk, operator_mid=ADMIN_MID)
        with pytest.raises(ValueError):
            await FolderCoverAuditService.approve(s, audit.pk, operator_mid=ADMIN_MID)


# ==================== 审核驳回 ====================


async def test_reject_keeps_cover_and_notifies(monkeypatch):
    _ok_verify(monkeypatch)
    async with new_session() as s:
        folder_id, _ = await _create_folder(s, C_MID)
        await FavoriteFolderAction(s, C_MID).update(folder_id, cover_url=NEW_COVER)
        audit = await _latest_for_folder(s, folder_id)
        item = await FolderCoverAuditService.reject(
            s, audit.pk, operator_mid=ADMIN_MID, reason="图片违规"
        )
        assert item.auditStatus == FolderCoverAuditStatusEnum.REJECTED.value
        folder = await s.get(TFavoriteFolder, folder_id)
        assert folder.cover_url is None  # 保持原封面（无封面）
        row = await s.get(TFolderCoverAudit, audit.pk)
        assert row.auditStatus is FolderCoverAuditStatusEnum.REJECTED
        assert row.auditReason == "图片违规"
        assert row.auditOperatorMid == ADMIN_MID

    async with new_session() as s2:
        rows = (
            await s2.exec(
                select(NotifyMessage).where(
                    NotifyMessage.target_type == NotifyTargetTypeEnum.CUSTOM,
                    NotifyMessage.target_value == str(C_MID),
                )
            )
        ).all()
        assert any("收藏夹封面审核未通过" in (r.title or "") for r in rows)


# ==================== 我的审核状态 ====================


async def test_mine_returns_latest(monkeypatch):
    _ok_verify(monkeypatch)
    async with new_session() as s:
        folder_id, _ = await _create_folder(s, C_MID, cover_url=NEW_COVER)
        mine = await FolderCoverAuditService.mine(s, uid=C_MID, folder_id=folder_id)
        assert mine is not None
        assert mine.auditStatus == FolderCoverAuditStatusEnum.PENDING.value
        assert mine.newCover == NEW_COVER


async def test_mine_none_when_no_record(monkeypatch):
    _ok_verify(monkeypatch)
    async with new_session() as s:
        folder_id = await generate_moment_id()
        mine = await FolderCoverAuditService.mine(s, uid=C_MID, folder_id=folder_id)
        assert mine is None


__all__ = [
    "test_approve_twice_raises",
    "test_approve_writes_cover_and_notifies",
    "test_create_folder_cover_verify_failure",
    "test_create_folder_with_cover_goes_pending",
    "test_create_folder_without_cover_no_audit",
    "test_mine_none_when_no_record",
    "test_mine_returns_latest",
    "test_pending_list_only_pending",
    "test_reject_keeps_cover_and_notifies",
    "test_resubmit_overrides_old_pending",
    "test_update_folder_clear_cover",
    "test_update_folder_cover_goes_pending",
    "test_update_folder_cover_verify_failure",
]
