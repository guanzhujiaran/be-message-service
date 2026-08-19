"""事件领域删除（`cleanup_event`）。

清除指定 uid 用户的事件提醒相关全部数据：

- `msg_event`(mid/actor_mid)：事件本体（接收者 / 触发者双向）；
- `msg_event_cursor`(mid)：事件拉取游标。
"""

from sqlalchemy import or_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col, delete

from app.models.db import EventMessage, EventReadCursor


class CleanupEventService:
    """事件领域删除（静态方法集合）。"""

    @staticmethod
    async def delete_all_by_uid(session: AsyncSession, uid: int) -> None:
        """删除指定 uid 用户的全部事件数据（调用方负责 commit）。"""
        await session.exec(
            delete(EventMessage).where(
                or_(col(EventMessage.mid) == uid, col(EventMessage.actor_mid) == uid)
            )
        )
        await session.exec(delete(EventReadCursor).where(col(EventReadCursor.mid) == uid))


__all__ = ["CleanupEventService"]
