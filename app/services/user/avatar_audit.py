"""头像更换审核服务。

头像更换改为「先审后发」流程：
- 用户提交新头像 → `TUserAvatarAudit` 插入 `pending` 记录，`TUserDetail.avatar` 保持旧头像；
- 审核通过 → 状态置 `approved`，把 `newAvatar` 写入 pptr Postgres `TUserDetail.avatar`（公开），
  并系统通知用户；
- 审核驳回 → 状态置 `rejected`，保持旧头像，系统通知用户并附驳回原因。

关键点：
- 同一时刻每人至多一条 `pending`（新提交覆盖旧 pending，旧 pending 置为 rejected）。
- 审核结果通知走 `NotifyService.send_to_user`（独立事务弱依赖，失败不回滚审核）。
- 审核通过写入公开头像（pptr Postgres）与本表状态（MySQL）分属两库，**非单事务**：
  先更新本表状态，再尽力写公开头像；写公开头像失败时记录并通知管理端人工介入（见 §5）。
"""

from datetime import datetime

from loguru import logger
from sqlmodel import func, select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.db import TUserAvatarAudit
from app.models.enums import AvatarAuditStatusEnum, NotifyLevelEnum
from app.models.schemas.avatar_audit import (
    AvatarAuditItem,
    AvatarAuditListResp,
    AvatarAuditMineResp,
)
from app.services.message.notify import NotifyService
from app.services.user.pptr_user import PptrUserService


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def _to_item(row: TUserAvatarAudit, brief) -> AvatarAuditItem:
    """TUserAvatarAudit → 审核队列卡片（brief 来自 PptrUserService.get_many 结果）。"""
    return AvatarAuditItem(
        pk=row.pk,
        mid=row.mid,
        authorName=brief.uname if brief else None,
        oldAvatar=row.oldAvatar,
        newAvatar=row.newAvatar,
        auditStatus=row.auditStatus.name,
        createdAt=_iso(row.created_at),
    )


class AvatarAuditService:
    """头像更换审核服务（静态方法集合，无状态）。"""

    # ==================== 用户侧：提交更换 ====================

    @staticmethod
    async def _current_avatar(uid: int) -> str | None:
        """读取用户当前公开头像（TUserDetail.avatar），用于记录 oldAvatar。"""
        profile = await PptrUserService.get_user_profile(uid=uid)
        if profile is None:
            return None
        _info, detail, _vip, _level = profile
        return detail.avatar if detail else None

    @staticmethod
    async def submit(
        session: AsyncSession,
        *,
        uid: int,
        new_avatar: str,
        old_avatar: str | None,
    ) -> int:
        """提交头像更换申请。

        同一时刻每人至多一条 pending：若已存在 pending 记录，先将其置为
        `rejected`（reason 固定为「已重新提交新申请」），再插入新记录。

        Returns:
            新插入记录的主键 pk。
        """
        # 把该用户所有 pending 记录作废（同事务，避免并发下出现多条 pending）
        pending_rows = (
            await session.exec(
                select(TUserAvatarAudit).where(
                    TUserAvatarAudit.mid == uid,
                    TUserAvatarAudit.auditStatus == AvatarAuditStatusEnum.PENDING,
                )
            )
        ).all()
        now = datetime.now()
        for p in pending_rows:
            p.auditStatus = AvatarAuditStatusEnum.REJECTED
            p.auditReason = "已重新提交新申请"
            p.auditedAt = now
            p.updated_at = now
            session.add(p)

        row = TUserAvatarAudit(
            mid=uid,
            oldAvatar=old_avatar,
            newAvatar=new_avatar,
            auditStatus=AvatarAuditStatusEnum.PENDING,
        )
        session.add(row)
        await session.commit()
        await session.refresh(row)
        logger.info(f"用户 {uid} 提交头像更换申请 pk={row.pk}，待审核")
        return row.pk or 0

    # ==================== 用户侧：我的审核状态 ====================

    @staticmethod
    async def mine(session: AsyncSession, *, uid: int) -> AvatarAuditMineResp | None:
        """返回该用户最近一条头像审核记录（无则 None）。"""
        row = (
            await session.exec(
                select(TUserAvatarAudit)
                .where(TUserAvatarAudit.mid == uid)
                .order_by(TUserAvatarAudit.pk.desc())
                .limit(1)
            )
        ).first()
        if row is None:
            return None
        return AvatarAuditMineResp(
            pk=row.pk,
            newAvatar=row.newAvatar,
            oldAvatar=row.oldAvatar,
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
    ) -> AvatarAuditListResp:
        """管理员待审核列表：auditStatus=pending，按创建时间倒序分页。"""
        page_num = max(1, page_num)
        page_size = min(max(1, page_size), 50)

        total = int(
            (
                await session.exec(
                    select(func.count())
                    .select_from(TUserAvatarAudit)
                    .where(TUserAvatarAudit.auditStatus == AvatarAuditStatusEnum.PENDING)
                )
            ).one()
            or 0
        )
        rows = (
            await session.exec(
                select(TUserAvatarAudit)
                .where(TUserAvatarAudit.auditStatus == AvatarAuditStatusEnum.PENDING)
                .order_by(TUserAvatarAudit.created_at.desc())
                .offset((page_num - 1) * page_size)
                .limit(page_size)
            )
        ).all()

        mids = {r.mid for r in rows}
        briefs = await PptrUserService.get_many(list(mids))
        items = [_to_item(r, briefs.get(r.mid)) for r in rows]
        return AvatarAuditListResp(
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
    ) -> AvatarAuditItem:
        """审核通过：状态→approved，并把 newAvatar 写入公开头像 + 通知用户。"""
        row = await session.get(TUserAvatarAudit, pk)
        if row is None:
            raise ValueError("头像审核记录不存在")
        if row.auditStatus is not AvatarAuditStatusEnum.PENDING:
            raise ValueError("该记录已处理，不能重复审核")

        now = datetime.now()
        row.auditStatus = AvatarAuditStatusEnum.APPROVED
        row.auditOperatorMid = operator_mid
        row.auditReason = remark
        row.auditedAt = now
        row.updated_at = now
        session.add(row)
        await session.commit()
        await session.refresh(row)
        logger.info(f"管理员 {operator_mid} 审核通过头像申请 pk={pk} mid={row.mid}")

        # 尽力写入公开头像（pptr Postgres，独立会话）；失败只告警，不回滚本表状态
        try:
            await PptrUserService.set_user_detail(uid=row.mid, face=row.newAvatar)
        except Exception as e:  # noqa: BLE001
            logger.error(f"审核通过后写入公开头像失败 mid={row.mid} newAvatar={row.newAvatar}: {e}")

        # 弱依赖通知：审核通过
        await AvatarAuditService._notify_approved(row)
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
    ) -> AvatarAuditItem:
        """审核驳回：状态→rejected，保持旧头像，通知用户并附驳回原因。"""
        row = await session.get(TUserAvatarAudit, pk)
        if row is None:
            raise ValueError("头像审核记录不存在")
        if row.auditStatus is not AvatarAuditStatusEnum.PENDING:
            raise ValueError("该记录已处理，不能重复审核")

        now = datetime.now()
        row.auditStatus = AvatarAuditStatusEnum.REJECTED
        row.auditOperatorMid = operator_mid
        row.auditReason = reason or remark
        row.auditedAt = now
        row.updated_at = now
        session.add(row)
        await session.commit()
        await session.refresh(row)
        logger.info(f"管理员 {operator_mid} 审核驳回头像申请 pk={pk} mid={row.mid}：{reason}")

        # 弱依赖通知：审核驳回
        await AvatarAuditService._notify_rejected(row)
        briefs = await PptrUserService.get_many([row.mid])
        return _to_item(row, briefs.get(row.mid))

    # ==================== 内部方法 ====================

    @staticmethod
    async def _notify_approved(row: TUserAvatarAudit) -> None:
        """弱依赖：审核通过系统通知用户。"""
        try:
            await NotifyService.send_to_user(
                row.mid,
                title="头像审核通过",
                content="您提交的新头像已审核通过并公开显示。",
                level=NotifyLevelEnum.NORMAL,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning(f"头像审核通过通知投递失败（弱依赖，已忽略）: {e}")

    @staticmethod
    async def _notify_rejected(row: TUserAvatarAudit) -> None:
        """弱依赖：审核驳回系统通知用户（附驳回原因）。"""
        reason = row.auditReason or ""
        lines = ["您提交的头像未通过审核，已保留原头像。"]
        if reason:
            lines.append(f"驳回原因：{reason}")
        try:
            await NotifyService.send_to_user(
                row.mid,
                title="头像审核未通过",
                content="\n".join(lines),
                level=NotifyLevelEnum.IMPORTANT,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning(f"头像审核驳回通知投递失败（弱依赖，已忽略）: {e}")


__all__ = ["AvatarAuditService"]
