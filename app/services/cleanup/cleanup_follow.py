"""关注领域删除（`cleanup_follow`）。

清除指定 uid 用户的关注 / 拉黑关系 `msg_user_follow`（双向）：

- `mid = uid`：我关注/拉黑的人；
- `target_mid = uid`：关注/拉黑我的人。

注销后对方的关注列表 / 粉丝列表应同步清除对该用户的引用。
"""

from sqlalchemy import or_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col, delete

from app.models.db import UserFollow


class CleanupFollowService:
    """关注领域删除（静态方法集合）。"""

    @staticmethod
    async def delete_all_by_uid(session: AsyncSession, uid: int) -> None:
        """删除指定 uid 用户的全部关注 / 拉黑关系（调用方负责 commit）。"""
        await session.exec(
            delete(UserFollow).where(
                or_(col(UserFollow.mid) == uid, col(UserFollow.target_mid) == uid)
            )
        )


__all__ = ["CleanupFollowService"]
