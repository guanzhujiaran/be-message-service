"""用户活跃度服务。

活跃度仅用于**判定用户当前是否在线**（驱动前端轮询站内信的节奏），
与「消息送达」无关——站内信（系统通知 / 事件提醒 / 私信）的送达完全由数据库
写路径保证，不存在「实时 / 批量推第三方」的概念。

活跃度写入非常频繁，因此用 MySQL 的 `INSERT ... ON DUPLICATE KEY UPDATE`
单条 SQL 完成 upsert，不做「先查后写」，避免并发下的竞态与额外往返。
"""

from datetime import datetime, timedelta

from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.core.config import settings
from app.models.db import UserActivity
from app.models.schemas import UserActivityResp


class ActivityService:
    """用户活跃度记录与判定。"""

    @staticmethod
    async def touch(session: AsyncSession, mid: int, commit: bool = True) -> None:
        """记录一次用户活跃（拉取消息、发私信、进入会话等都算）。"""
        from sqlalchemy.dialects.mysql import insert as mysql_insert

        now = datetime.now()
        stmt = mysql_insert(UserActivity.__table__).values(
            mid=mid,
            last_active_at=now,
            active_count=1,
            created_at=now,
            updated_at=now,
        )
        stmt = stmt.on_duplicate_key_update(
            last_active_at=now,
            active_count=UserActivity.__table__.c.active_count + 1,
            updated_at=now,
        )
        await session.exec(stmt)  # type: ignore[call-overload]
        if commit:
            await session.commit()

    @staticmethod
    async def is_active(session: AsyncSession, mid: int) -> bool:
        """判断用户当前是否处于活跃窗口内。"""
        threshold = datetime.now() - timedelta(
            seconds=settings.active_user_window_seconds
        )
        stmt = select(UserActivity.last_active_at).where(UserActivity.mid == mid)
        last_active = (await session.exec(stmt)).one_or_none()
        return last_active is not None and last_active >= threshold

    @staticmethod
    async def get_snapshot(session: AsyncSession, mid: int) -> UserActivityResp:
        stmt = select(UserActivity).where(UserActivity.mid == mid)
        row = (await session.exec(stmt)).one_or_none()
        if row is None:
            return UserActivityResp(
                mid=mid,
                last_active_at=datetime.fromtimestamp(0),
                active_count=0,
                is_active=False,
            )
        threshold = datetime.now() - timedelta(
            seconds=settings.active_user_window_seconds
        )
        return UserActivityResp(
            mid=row.mid,
            last_active_at=row.last_active_at,
            active_count=row.active_count,
            is_active=row.last_active_at >= threshold,
        )

    @staticmethod
    async def count_active(session: AsyncSession) -> int:
        """统计当前活跃用户数（运维观测用）。"""
        from sqlalchemy import func

        threshold = datetime.now() - timedelta(
            seconds=settings.active_user_window_seconds
        )
        stmt = select(func.count()).select_from(UserActivity).where(
            UserActivity.last_active_at >= threshold
        )
        return int((await session.exec(stmt)).one() or 0)


__all__ = ["ActivityService"]
