"""收藏领域删除（`cleanup_favorite`）。

清除指定 uid 用户的收藏相关全部数据：

- `TFavoriteFolder`(mid)：收藏夹（含逐夹 `is_public` 可见性）；
- `TResourceFavorite`(mid)：收藏明细（收藏了哪些动态）；
- `TFolderCoverAudit`(mid)：收藏夹封面审核记录（2.28.0）。

注：全局收藏开关表 `TUserFavoriteSetting` 已于 2026-10-08 删除（改为逐夹可见性）。
"""

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col, delete

from app.models.db import (
    TFavoriteFolder,
    TFolderCoverAudit,
    TResourceFavorite,
)


class CleanupFavoriteService:
    """收藏领域删除（静态方法集合）。"""

    @staticmethod
    async def delete_all_by_uid(session: AsyncSession, uid: int) -> None:
        """删除指定 uid 用户的全部收藏数据（调用方负责 commit）。"""
        for model in (
            TFavoriteFolder,
            TResourceFavorite,
            TFolderCoverAudit,
        ):
            await session.exec(delete(model).where(col(model.mid) == uid))


__all__ = ["CleanupFavoriteService"]
