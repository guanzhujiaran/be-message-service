"""用户注销冷静期状态服务（两阶段注销的状态编排）。

所有方法都接收调用方传入的 MySQL `AsyncSession`，由调用方决定事务边界（提交/回滚），
与项目内其它领域服务的约定一致。

状态机：
    (无记录)
       │ submit_deactivation
       ▼
    cooling ──auto_revoke_if_cooling──► (无记录，账号恢复)
       │ 到期任务：本地物理删成功后 mark_pending_casdoor
       ▼
    pending_casdoor ──Casdoor 删除成功 clear──► (无记录)
       │ Casdoor 删除失败：mark_casdoor_error（retry_count+1，补偿任务重试）
"""

from __future__ import annotations

import datetime

from loguru import logger
from sqlmodel import col, select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.core.config import settings
from app.models.db.deactivation_tbl import UserDeactivation

COOLING = "cooling"
PENDING_CASDOOR = "pending_casdoor"


async def get_cooling(session: AsyncSession, mid: int) -> UserDeactivation | None:
    """取某用户的冷静期记录；非 cooling（如 pending_casdoor）或无记录返回 None。"""
    row = (
        await session.exec(
            select(UserDeactivation).where(col(UserDeactivation.mid) == mid)
        )
    ).first()
    if row and row.status == COOLING:
        return row
    return None


async def is_deactivated(session: AsyncSession, mid: int) -> bool:
    """是否处于冷静期（供认证拦截 / /identify 查询）。"""
    return await get_cooling(session, mid) is not None


async def submit_deactivation(
    session: AsyncSession, mid: int, user_name: str | None = None
) -> UserDeactivation:
    """提交注销（幂等）：已有 cooling 记录则沿用，不重复延后到期时间。

    到期时间 = now + account_deactivate_grace_days 天。
    """
    existing = (
        await session.exec(
            select(UserDeactivation).where(col(UserDeactivation.mid) == mid)
        )
    ).first()
    if existing:
        if existing.status == COOLING:
            logger.info(f"[deactivation] 用户 {mid} 已在冷静期，沿用原记录")
            return existing
        # pending_casdoor（本地已删）不应再出现提交，防护性报错
        raise ValueError(f"用户 {mid} 注销记录处于 {existing.status}，不可重复提交")

    now = datetime.datetime.now()
    delete_after = now + datetime.timedelta(
        days=settings.account_deactivate_grace_days
    )
    row = UserDeactivation(
        mid=mid,
        user_name=user_name,
        status=COOLING,
        deactivated_at=now,
        delete_after=delete_after,
    )
    session.add(row)
    logger.info(
        f"[deactivation] 用户 {mid} 提交注销，冷静期至 {delete_after:%Y-%m-%d %H:%M:%S}"
    )
    return row


async def auto_revoke_if_cooling(session: AsyncSession, mid: int) -> bool:
    """登录自动撤销：若处于冷静期则删除注销记录。返回是否发生了撤销。"""
    row = await get_cooling(session, mid)
    if row is None:
        return False
    await session.delete(row)
    logger.info(f"[deactivation] 用户 {mid} 冷静期内重新登录，自动撤销注销")
    return True


async def list_due(
    session: AsyncSession, now: datetime.datetime, limit: int = 100
) -> list[UserDeactivation]:
    """扫描到期的冷静期账号（status=cooling 且 delete_after<=now）。"""
    return list(
        (
            await session.exec(
                select(UserDeactivation)
                .where(col(UserDeactivation.status) == COOLING)
                .where(col(UserDeactivation.delete_after) <= now)
                .order_by(col(UserDeactivation.delete_after))
                .limit(limit)
            )
        ).all()
    )


async def list_pending_casdoor(
    session: AsyncSession, limit: int = 100
) -> list[UserDeactivation]:
    """扫描本地已删、Casdoor 待删的补偿记录。"""
    return list(
        (
            await session.exec(
                select(UserDeactivation)
                .where(col(UserDeactivation.status) == PENDING_CASDOOR)
                .order_by(col(UserDeactivation.updated_at))
                .limit(limit)
            )
        ).all()
    )


async def mark_pending_casdoor(
    session: AsyncSession, row: UserDeactivation
) -> None:
    """本地物理删除成功后，把记录置为 Casdoor 待删补偿态。"""
    row.status = PENDING_CASDOOR
    row.casdoor_delete_error = None
    session.add(row)


async def mark_casdoor_error(
    session: AsyncSession, row: UserDeactivation, error: str
) -> None:
    """记录一次 Casdoor 删除失败，retry_count + 1，截断错误信息。"""
    row.status = PENDING_CASDOOR
    row.retry_count = (row.retry_count or 0) + 1
    row.casdoor_delete_error = (error or "")[:512]
    session.add(row)


async def clear_after_casdoor_deleted(
    session: AsyncSession, row: UserDeactivation
) -> None:
    """Casdoor 删除成功，删除注销记录（表保持精简）。"""
    await session.delete(row)


__all__ = [
    "COOLING",
    "PENDING_CASDOOR",
    "auto_revoke_if_cooling",
    "clear_after_casdoor_deleted",
    "get_cooling",
    "is_deactivated",
    "list_due",
    "list_pending_casdoor",
    "mark_casdoor_error",
    "mark_pending_casdoor",
    "submit_deactivation",
]
