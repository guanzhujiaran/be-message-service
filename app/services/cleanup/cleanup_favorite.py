"""收藏领域删除（`cleanup_favorite`）。

清除指定 uid 用户的收藏相关全部数据：

- `TFavoriteFolder`(mid)：收藏夹；
- `TMomentFavorite`(mid)：收藏明细（收藏了哪些动态）；
- `TUserFavoriteSetting`(mid)：主页收藏可见性设置；
- `TFolderCoverAudit`(mid)：收藏夹封面审核记录（2.28.0）。
"""

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col, delete

from app.models.db import TFavoriteFolder, TFolderCoverAudit, TMomentFavorite, TUserFavoriteSetting


class CleanupFavoriteService:
    """收藏领域删除（静态方法集合）。"""

    @staticmethod
    async def delete_all_by_uid(session: AsyncSession, uid: int) -> None:
        """删除指定 uid 用户的全部收藏数据（调用方负责 commit）。"""
        for model in (TFavoriteFolder, TMomentFavorite, TUserFavoriteSetting, TFolderCoverAudit):
            await session.exec(delete(model).where(col(model.mid) == uid))


__all__ = ["CleanupFavoriteService"]
