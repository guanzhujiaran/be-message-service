"""通知领域删除（`cleanup_notify`）。

清除指定 uid 用户的通知相关全部数据：

- `msg_notify_cursor`(mid)：通知拉取游标（用户维度必删）；
- `msg_notify_state`(mid)：通知已读状态；
- `msg_notify`(creator_mid)：该用户作为发布者产生的通知本体。
"""

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col, delete

from app.models.db import NotifyCursor, NotifyMessage, NotifyState


class CleanupNotifyService:
    """通知领域删除（静态方法集合）。"""

    @staticmethod
    async def delete_all_by_uid(session: AsyncSession, uid: int) -> None:
        """删除指定 uid 用户的全部通知数据（调用方负责 commit）。"""
        await session.exec(delete(NotifyCursor).where(col(NotifyCursor.mid) == uid))
        await session.exec(delete(NotifyState).where(col(NotifyState.mid) == uid))
        await session.exec(
            delete(NotifyMessage).where(col(NotifyMessage.creator_mid) == uid)
        )


__all__ = ["CleanupNotifyService"]
