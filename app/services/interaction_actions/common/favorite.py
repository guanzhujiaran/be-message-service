"""非动态资源通用收藏互动（2.47.0 对象化）。

资源存在性经注册式校验器校验；同一资源可收藏到多个夹，favoriteCount 按用户去重
（用户首次收藏该资源才 +1，仅当所有夹都不再收藏才 -1），明细写 `TMomentFavorite`。
"""

from sqlmodel import col, delete, select

from app.models.db import TFavoriteFolder, TMomentFavorite
from app.models.schemas.interaction import InteractionResource
from app.services.moment.interaction import (
    BeMessageInteractionStatService as InteractionStatService,
    InteractionResourceValidator,
)
from app.services.interaction_actions.base import (
    BaseInteractionAction,
    InteractionActionError,
    InteractionRelationScopeEnum,
)


class ResourceFavoriteAction(BaseInteractionAction):
    """非动态资源通用收藏 / 取消收藏（幂等；多夹 + 用户去重计数）。

    子类需声明 `_biz_type`（如 `lottery` / `rpa_action` 等）。
    """

    relation_scope = [InteractionRelationScopeEnum.NOT_BLOCKED]
    error_messages = {
        "not_found": "资源不存在",
        "folder_not_found": "收藏夹不存在",
        "invalid": "action 参数不合法（add/remove）",
    }

    def __init__(
        self,
        session,
        actor_mid: int,
        biz_id: int,
        folder_id=None,
        action: str = "add",
        **ids,
    ):
        super().__init__(session, actor_mid, biz_id, **ids)
        self.folder_id = folder_id
        self.action = action

    # ==================== 基类接口 ====================

    async def get_resource(self) -> InteractionResource:
        """资源存在性校验（校验失败抛错）；通过后返回统一资源表示（作者未知 → None）。"""
        await InteractionResourceValidator.validate(self.session, self.biz_type, self.biz_id)
        return InteractionResource(
            bizType=self.biz_type,
            bizId=self.biz_id,
            exists=True,
            interactable=True,
        )

    async def do_execute(self, resource):
        if self.action == "add":
            return await self._add()
        if self.action == "remove":
            return await self._remove()
        raise InteractionActionError(self.error_messages["invalid"])

    # ==================== 收藏 / 取消实现 ====================

    async def _ensure_default_folder(self) -> int:
        """确保默认收藏夹存在，返回其 folder_id（get_or_create，并发安全）。

        委托收藏夹管理操作类 `FavoriteFolderAction`。
        """
        from app.services.interaction_actions.folder import FavoriteFolderAction

        return await FavoriteFolderAction(self.session, self.actor_mid).ensure_default()

    async def _add(self) -> tuple[bool, int]:
        """收藏资源到指定收藏夹（幂等）。返回 (是否新增收藏, 目标 folder_id)。"""
        # 未指定收藏夹：自动使用（创建）默认收藏夹
        if self.folder_id is None:
            folder_id = await self._ensure_default_folder()
        else:
            folder_id = int(self.folder_id)
        # 收藏夹归属校验
        folder = (
            await self.session.exec(
                select(TFavoriteFolder).where(
                    col(TFavoriteFolder.folder_id) == folder_id,
                    col(TFavoriteFolder.mid) == self.actor_mid,
                )
            )
        ).one_or_none()
        if folder is None:
            raise InteractionActionError(self.error_messages["folder_not_found"])

        exists = (
            await self.session.exec(
                select(TMomentFavorite.pk).where(
                    col(TMomentFavorite.bizType) == self.biz_type,
                    col(TMomentFavorite.bizId) == self.biz_id,
                    col(TMomentFavorite.folderId) == folder_id,
                )
            )
        ).first()
        if exists is not None:
            await self.session.commit()
            return False, folder_id

        self.session.add(
            TMomentFavorite(
                bizType=self.biz_type,
                bizId=self.biz_id,
                dynId=None,
                folderId=folder_id,
                mid=self.actor_mid,
            )
        )
        await self.session.flush()

        # 该用户是否已在其它夹收藏过同一资源？没有才计数 +1（用户去重）
        already = (
            await self.session.exec(
                select(TMomentFavorite.pk).where(
                    col(TMomentFavorite.mid) == self.actor_mid,
                    col(TMomentFavorite.bizType) == self.biz_type,
                    col(TMomentFavorite.bizId) == self.biz_id,
                    col(TMomentFavorite.folderId) != folder_id,
                )
            )
        ).first()
        if already is None:
            await InteractionStatService.incr(
                self.session, self.biz_type, self.biz_id, "favoriteCount", 1
            )
        await self.session.commit()
        return True, folder_id

    async def _remove(self) -> tuple[bool, int]:
        """从指定收藏夹取消收藏（幂等）。返回 (是否删除收藏, 目标 folder_id)。"""
        if self.folder_id is None:
            raise InteractionActionError(self.error_messages["invalid"])
        folder_id = int(self.folder_id)
        row = (
            await self.session.exec(
                select(TMomentFavorite).where(
                    col(TMomentFavorite.bizType) == self.biz_type,
                    col(TMomentFavorite.bizId) == self.biz_id,
                    col(TMomentFavorite.folderId) == folder_id,
                    col(TMomentFavorite.mid) == self.actor_mid,
                )
            )
        ).first()
        if row is None:
            await self.session.commit()
            return False, folder_id
        await self.session.exec(
            delete(TMomentFavorite).where(
                col(TMomentFavorite.bizType) == self.biz_type,
                col(TMomentFavorite.bizId) == self.biz_id,
                col(TMomentFavorite.folderId) == folder_id,
                col(TMomentFavorite.mid) == self.actor_mid,
            )
        )
        # 该用户是否还在其它夹收藏同一资源？没有才计数 -1
        other = (
            await self.session.exec(
                select(TMomentFavorite.pk).where(
                    col(TMomentFavorite.mid) == self.actor_mid,
                    col(TMomentFavorite.bizType) == self.biz_type,
                    col(TMomentFavorite.bizId) == self.biz_id,
                    col(TMomentFavorite.folderId) != folder_id,
                )
            )
        ).first()
        if other is None:
            await InteractionStatService.decr(
                self.session, self.biz_type, self.biz_id, "favoriteCount"
            )
        await self.session.commit()
        return True, folder_id


__all__ = ["ResourceFavoriteAction"]
