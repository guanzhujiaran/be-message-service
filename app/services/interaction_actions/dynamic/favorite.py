"""动态（DYNAMIC）收藏互动操作（2.47.0 对象化；归入 `dynamic/` 子包）。

动态资源专属：多夹收藏 + 用户去重计数。
"""

from sqlmodel import col, delete, select

from app.models.db import TFavoriteFolder, TMoment, TMomentFavorite
from app.models.enums import InteractionBizTypeEnum
from app.models.schemas.interaction import InteractionResource
from app.services.interaction_actions.base import (
    BaseInteractionAction,
    InteractionActionError,
    InteractionRelationScopeEnum,
)
from app.services.moment.moment_stat import MomentStatService


class FavoriteAction(BaseInteractionAction):
    """动态收藏 / 取消收藏（幂等；同一动态可收藏到多个夹，favoriteCount 按用户去重）。

    - 资源类型：`_biz_type = DYNAMIC`（不可变，类声明绑定）；
    - ``action="add"``：收藏到指定夹（缺省默认夹），返回 ``(是否新增收藏, 目标 folder_id)``；
    - ``action="remove"``：从指定夹取消收藏，返回 ``(是否删除收藏, 目标 folder_id)``。

    关系权限：`[NOT_BLOCKED]`（任一向黑名单禁止收藏）。
    """

    _biz_type = InteractionBizTypeEnum.DYNAMIC
    relation_scope = [InteractionRelationScopeEnum.NOT_BLOCKED]
    error_messages = {
        "not_found": "动态不存在",
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
        """取 TMoment 并折叠为统一资源表示（收藏语义：未软删即可互动，不校验 auditStatus）。"""
        dyn = (
            await self.session.exec(
                select(TMoment).where(col(TMoment.dynId) == self.biz_id)
            )
        ).one_or_none()
        if dyn is None or dyn.deletedAt is not None:
            return InteractionResource(
                bizType=InteractionBizTypeEnum.DYNAMIC,
                bizId=self.biz_id,
                exists=False,
            )
        return InteractionResource(
            bizType=InteractionBizTypeEnum.DYNAMIC,
            bizId=self.biz_id,
            authorMid=int(dyn.mid),
            exists=True,
            interactable=True,
            content=dyn.contentText,
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
        """收藏动态到指定收藏夹（幂等）。返回 (是否新增收藏, 目标 folder_id)。"""
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
                    col(TMomentFavorite.bizType) == InteractionBizTypeEnum.DYNAMIC,
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
                bizType=InteractionBizTypeEnum.DYNAMIC,
                bizId=self.biz_id,
                dynId=self.biz_id,
                folderId=folder_id,
                mid=self.actor_mid,
            )
        )
        await self.session.flush()

        # 该用户是否已在其它夹收藏过同一动态？没有才计数 +1（用户去重）
        already = (
            await self.session.exec(
                select(TMomentFavorite.pk).where(
                    col(TMomentFavorite.mid) == self.actor_mid,
                    col(TMomentFavorite.bizType) == InteractionBizTypeEnum.DYNAMIC,
                    col(TMomentFavorite.bizId) == self.biz_id,
                    col(TMomentFavorite.folderId) != folder_id,
                )
            )
        ).first()
        if already is None:
            await MomentStatService.incr_stat(self.session, self.biz_id, "favoriteCount", 1)
        await self.session.commit()
        return True, folder_id

    async def _remove(self) -> tuple[bool, int]:
        """从指定收藏夹取消收藏（幂等）。返回 (是否删除收藏, 目标 folder_id)。"""
        folder_id = int(self.folder_id)
        row = (
            await self.session.exec(
                select(TMomentFavorite).where(
                    col(TMomentFavorite.bizType) == InteractionBizTypeEnum.DYNAMIC,
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
                col(TMomentFavorite.bizType) == InteractionBizTypeEnum.DYNAMIC,
                col(TMomentFavorite.bizId) == self.biz_id,
                col(TMomentFavorite.folderId) == folder_id,
                col(TMomentFavorite.mid) == self.actor_mid,
            )
        )
        # 该用户是否还在其它夹收藏同一动态？没有才计数 -1
        other = (
            await self.session.exec(
                select(TMomentFavorite.pk).where(
                    col(TMomentFavorite.mid) == self.actor_mid,
                    col(TMomentFavorite.bizType) == InteractionBizTypeEnum.DYNAMIC,
                    col(TMomentFavorite.bizId) == self.biz_id,
                    col(TMomentFavorite.folderId) != folder_id,
                )
            )
        ).first()
        if other is None:
            await MomentStatService.decr_stat(
                self.session, self.biz_id, "favoriteCount", floor_zero=True
            )
        await self.session.commit()
        return True, folder_id


__all__ = ["FavoriteAction"]
