"""收藏夹封面审核服务（对齐头像更换审核 `AvatarAuditService` 模式）。

收藏夹封面「先审后发」流程：
- 用户提交封面（创建/更新收藏夹携带 `coverUrl`，经 `verify_avatar_url` 下载校验）→
  `TFolderCoverAudit` 插入 `pending` 记录，`TFavoriteFolder.cover_url` 保持原封面（新夹为空）；
- 审核通过 → 状态置 `approved`，把 `newCover` 写入 `TFavoriteFolder.cover_url`（对外公开），
  并系统通知用户；
- 审核驳回 → 状态置 `rejected`，保持原封面，系统通知用户并附驳回原因。

关键点：
- 同一收藏夹至多一条 `pending`（新提交覆盖旧 pending，旧 pending 置为 rejected）。
- 审核通过写 `cover_url` 与本表状态**同库同事务**（与头像审核跨库不同，无中间态）。
- 审核结果通知走 `NotifyService.send_to_user`（独立事务弱依赖，失败不回滚审核）。
"""

from datetime import datetime

from loguru import logger
from sqlmodel import col, func, select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.db import TFavoriteFolder, TFolderCoverAudit
from app.models.enums import FolderCoverAuditStatusEnum, NotifyLevelEnum
from app.models.schemas.folder_cover_audit import (
    FolderCoverAuditItem,
    FolderCoverAuditListResp,
    FolderCoverAuditMineResp,
)
from app.services.notify import NotifyService
from app.services.pptr_user import PptrUserService


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def _to_item(row: TFolderCoverAudit, brief) -> FolderCoverAuditItem:
    """TFolderCoverAudit → 审核队列卡片（brief 来自 PptrUserService.get_many 结果）。"""
    return FolderCoverAuditItem(
        pk=row.pk,
        folderId=str(row.folderId),
        mid=row.mid,
        authorName=brief.uname if brief else None,
        oldCover=row.oldCover,
        newCover=row.newCover,
        auditStatus=row.auditStatus.name,
        createdAt=_iso(row.created_at),
    )


class FolderCoverAuditService:
    """收藏夹封面审核服务（静态方法集合，无状态）。"""

    # ==================== 用户侧：提交封面 ====================

    @staticmethod
    async def submit(
        session: AsyncSession,
        *,
        uid: int,
        folder_id: int,
        new_cover: str,
        old_cover: str | None,
    ) -> int:
        """提交收藏夹封面审核申请。

        同一收藏夹至多一条 pending：若该夹已存在 pending 记录，先将其置为
        `rejected`（reason 固定为「已重新提交新申请」），再插入新记录（同事务）。

        Returns:
            新插入记录的主键 pk。
        """
        # 把该夹所有 pending 记录作废（同事务，避免并发下出现多条 pending）
        pending_rows = (
            await session.exec(
                select(TFolderCoverAudit).where(
                    TFolderCoverAudit.folderId == folder_id,
                    TFolderCoverAudit.auditStatus == FolderCoverAuditStatusEnum.PENDING,
                )
            )
        ).all()
        now = datetime.now()
        for p in pending_rows:
            p.auditStatus = FolderCoverAuditStatusEnum.REJECTED
            p.auditReason = "已重新提交新申请"
            p.auditedAt = now
            p.updated_at = now
            session.add(p)

        row = TFolderCoverAudit(
            folderId=folder_id,
            mid=uid,
            oldCover=old_cover,
            newCover=new_cover,
            auditStatus=FolderCoverAuditStatusEnum.PENDING,
        )
        session.add(row)
        await session.commit()
        await session.refresh(row)
        logger.info(f"用户 {uid} 提交收藏夹封面审核申请 folderId={folder_id} pk={row.pk}，待审核")
        return row.pk or 0

    # ==================== 用户侧：我的审核状态 ====================

    @staticmethod
    async def mine(
        session: AsyncSession, *, uid: int, folder_id: int
    ) -> FolderCoverAuditMineResp | None:
        """返回某收藏夹最近一条封面审核记录（无则 None，仅本人可查）。"""
        row = (
            await session.exec(
                select(TFolderCoverAudit)
                .where(
                    TFolderCoverAudit.mid == uid,
                    TFolderCoverAudit.folderId == folder_id,
                )
                .order_by(TFolderCoverAudit.pk.desc())
                .limit(1)
            )
        ).first()
        if row is None:
            return None
        return FolderCoverAuditMineResp(
            pk=row.pk,
            folderId=str(row.folderId),
            newCover=row.newCover,
            oldCover=row.oldCover,
            auditStatus=row.auditStatus.name,
            auditReason=row.auditReason,
            createdAt=_iso(row.created_at),
            auditedAt=_iso(row.auditedAt),
        )

    # ==================== 管理端：待审核列表 ====================

    @staticmethod
    async def pending_list(
        session: AsyncSession,
        *,
        page_num: int = 1,
        page_size: int = 20,
    ) -> FolderCoverAuditListResp:
        """管理员待审核列表：auditStatus=pending，按创建时间倒序分页。"""
        page_num = max(1, page_num)
        page_size = min(max(1, page_size), 50)

        total = int(
            (
                await session.exec(
                    select(func.count())
                    .select_from(TFolderCoverAudit)
                    .where(TFolderCoverAudit.auditStatus == FolderCoverAuditStatusEnum.PENDING)
                )
            ).one()
            or 0
        )
        rows = (
            await session.exec(
                select(TFolderCoverAudit)
                .where(TFolderCoverAudit.auditStatus == FolderCoverAuditStatusEnum.PENDING)
                .order_by(TFolderCoverAudit.created_at.desc())
                .offset((page_num - 1) * page_size)
                .limit(page_size)
            )
        ).all()

        mids = {r.mid for r in rows}
        briefs = await PptrUserService.get_many(list(mids))
        items = [_to_item(r, briefs.get(r.mid)) for r in rows]
        return FolderCoverAuditListResp(
            items=items, total=total, page_num=page_num, page_size=page_size
        )

    # ==================== 管理端：审核通过 ====================

    @staticmethod
    async def approve(
        session: AsyncSession,
        pk: int,
        *,
        operator_mid: int,
        remark: str | None = None,
    ) -> FolderCoverAuditItem:
        """审核通过：状态→approved，并把 newCover 写入收藏夹封面（同库同事务）+ 通知用户。"""
        row = await session.get(TFolderCoverAudit, pk)
        if row is None:
            raise ValueError("封面审核记录不存在")
        if row.auditStatus is not FolderCoverAuditStatusEnum.PENDING:
            raise ValueError("该记录已处理，不能重复审核")

        now = datetime.now()
        row.auditStatus = FolderCoverAuditStatusEnum.APPROVED
        row.auditOperatorMid = operator_mid
        row.auditReason = remark
        row.auditedAt = now
        row.updated_at = now
        session.add(row)

        # 同库同事务：新封面写入公开收藏夹（保证状态与封面原子一致）
        folder = await session.get(TFavoriteFolder, row.folderId)
        if folder is None:
            await session.rollback()
            raise ValueError("收藏夹不存在")
        folder.cover_url = row.newCover
        session.add(folder)

        await session.commit()
        await session.refresh(row)
        logger.info(f"管理员 {operator_mid} 审核通过收藏夹封面申请 pk={pk} folderId={row.folderId} mid={row.mid}")

        # 弱依赖通知：审核通过
        await FolderCoverAuditService._notify_approved(row)
        briefs = await PptrUserService.get_many([row.mid])
        return _to_item(row, briefs.get(row.mid))

    # ==================== 管理端：审核驳回 ====================

    @staticmethod
    async def reject(
        session: AsyncSession,
        pk: int,
        *,
        operator_mid: int,
        reason: str,
        remark: str | None = None,
    ) -> FolderCoverAuditItem:
        """审核驳回：状态→rejected，保持原封面，通知用户并附驳回原因。"""
        row = await session.get(TFolderCoverAudit, pk)
        if row is None:
            raise ValueError("封面审核记录不存在")
        if row.auditStatus is not FolderCoverAuditStatusEnum.PENDING:
            raise ValueError("该记录已处理，不能重复审核")

        now = datetime.now()
        row.auditStatus = FolderCoverAuditStatusEnum.REJECTED
        row.auditOperatorMid = operator_mid
        row.auditReason = reason or remark
        row.auditedAt = now
        row.updated_at = now
        session.add(row)
        await session.commit()
        await session.refresh(row)
        logger.info(f"管理员 {operator_mid} 审核驳回收藏夹封面申请 pk={pk} folderId={row.folderId} mid={row.mid}：{reason}")

        # 弱依赖通知：审核驳回
        await FolderCoverAuditService._notify_rejected(row)
        briefs = await PptrUserService.get_many([row.mid])
        return _to_item(row, briefs.get(row.mid))

    # ==================== 内部方法 ====================

    @staticmethod
    async def _notify_approved(row: TFolderCoverAudit) -> None:
        """弱依赖：审核通过系统通知用户。"""
        try:
            await NotifyService.send_to_user(
                row.mid,
                title="收藏夹封面审核通过",
                content="您提交的收藏夹封面已审核通过并公开显示。",
                level=NotifyLevelEnum.NORMAL,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning(f"收藏夹封面审核通过通知投递失败（弱依赖，已忽略）: {e}")

    @staticmethod
    async def _notify_rejected(row: TFolderCoverAudit) -> None:
        """弱依赖：审核驳回系统通知用户（附驳回原因）。"""
        reason = row.auditReason or ""
        lines = ["您提交的收藏夹封面未通过审核，已保留原封面。"]
        if reason:
            lines.append(f"驳回原因：{reason}")
        try:
            await NotifyService.send_to_user(
                row.mid,
                title="收藏夹封面审核未通过",
                content="\n".join(lines),
                level=NotifyLevelEnum.IMPORTANT,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning(f"收藏夹封面审核驳回通知投递失败（弱依赖，已忽略）: {e}")


__all__ = ["FolderCoverAuditService"]
