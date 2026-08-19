"""动态领域删除（`cleanup_moment`）。

清除指定 uid 用户在 be-message MySQL 的动态相关全部数据：

- **发布的动态** `TMoment WHERE mid=uid`：`TMomentStat`/`TMomentLike`/
  `TMomentViewLog`/`TMomentReport`/`TMomentAuditLog` 对 `dynId` 均
  `ondelete=CASCADE`，删主表自动级联清子表；
- **点赞 / 浏览他人动态的痕迹**（`TMomentLike`/`TMomentViewLog` 的 `mid`）：
  这些行的 `mid` 是该用户（操作者），非动态作者，需单独按 `mid` 删，
  不会随其发布动态级联。

注意：本模块只操作 be-message 主库，调用方传入可用的 `AsyncSession`。
"""

from sqlalchemy import or_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col, delete

from app.models.db import TMoment, TMomentLike, TMomentViewLog


class CleanupMomentService:
    """动态领域删除（静态方法集合）。"""

    @staticmethod
    async def delete_all_by_uid(session: AsyncSession, uid: int) -> None:
        """删除指定 uid 用户的全部动态数据（调用方负责 commit）。

        按依赖顺序：先清「操作痕迹」（mid=操作者），再删其发布动态
        （dynId 子表自动 CASCADE）。
        """
        # 我点赞 / 浏览他人动态的痕迹（mid = 操作者）
        await session.exec(
            delete(TMomentLike).where(col(TMomentLike.mid) == uid)
        )
        await session.exec(
            delete(TMomentViewLog).where(col(TMomentViewLog.mid) == uid)
        )
        # 我发布的动态（dynId 子表自动 CASCADE）
        await session.exec(delete(TMoment).where(col(TMoment.mid) == uid))


__all__ = ["CleanupMomentService"]
