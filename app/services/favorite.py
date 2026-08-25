"""动态收藏服务（收藏夹系统）。

收藏夹业务约束（计划书 §4.2）：

- **多夹 + 默认夹**：一个用户可有多个收藏夹，且**每用户有且仅有一个默认收藏夹**
  （`is_default=1`）。新用户首次进入收藏时由 `ensure_default_folder` 自动创建，
  保证无需先建夹即可收藏。
- **收藏夹字段**：自定义封面（`cover_url`，**仅存 URL 不转存图片**）、名称、描述。
- **收藏明细**：`TMomentFavorite` 唯一约束 `(dynId, folderId)` 保证同一夹内
  同一动态不重复收藏；同一动态可被收藏到**多个不同夹**。
- **favoriteCount 语义（按用户去重）**：`TMomentStat.favoriteCount` 表示收藏该
  动态的**用户数**。用户首次收藏某动态（其它夹也未曾收藏过）时才 `+1`；
  仅当该用户在所有夹都不再收藏该动态时才 `-1`。避免同一用户多夹收藏虚增收藏数。
- **主页收藏可见性**：`TUserFavoriteSetting` 每用户一行，`showFavorites` 默认
  1（主页展示「收藏」tab），用户可在主页「设置」tab 关闭（设为 0）。
"""

from loguru import logger
from sqlalchemy import func
from sqlmodel import col, delete, select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.core.sharding import generate_moment_id
from app.models.db import (
    TFavoriteFolder,
    TFolderCoverAudit,
    TMomentFavorite,
    TUserFavoriteSetting,
)
from app.models.enums import FolderCoverAuditStatusEnum, InteractionBizTypeEnum
from app.services.avatar_check import verify_avatar_url
from app.services.folder_cover_audit import FolderCoverAuditService
from app.services.interaction import (
    BeMessageInteractionStatService as InteractionStatService,
    InteractionResourceValidator,
)
from app.services.moment_stat import MomentStatService


class FavoriteService:
    """收藏服务（静态方法集合，无状态；2.17.0 泛化支持任意业务资源）。

    `bizType` + `bizId` 唯一定位可收藏资源；动态资源（bizType=dynamic）时
    `bizId=dynId`，计数走 `TMomentStat`；非动态资源（lottery/rpa_*）计数走
    `TInteractionStat`。
    """

    # ==================== 收藏夹 CRUD ====================

    @staticmethod
    async def ensure_default_folder(session: AsyncSession, mid: int) -> int:
        """确保默认收藏夹存在，返回其 folder_id（get_or_create，并发安全）。

        首次进入收藏流程（创建/列表/收藏）时调用；每用户仅一个默认夹。
        """
        row = (
            await session.exec(
                select(TFavoriteFolder).where(
                    col(TFavoriteFolder.mid) == mid,
                    col(TFavoriteFolder.is_default) == 1,
                )
            )
        ).first()
        if row is not None:
            return row.folder_id
        # 并发兜底：另一请求刚创建了默认夹
        folder_id = await generate_moment_id()
        session.add(
            TFavoriteFolder(
                folder_id=folder_id,
                mid=mid,
                name="默认收藏夹",
                description="我的默认收藏夹",
                is_default=1,
            )
        )
        try:
            await session.commit()
            return folder_id
        except Exception:  # noqa: BLE001
            await session.rollback()
            row2 = (
                await session.exec(
                    select(TFavoriteFolder).where(
                        col(TFavoriteFolder.mid) == mid,
                        col(TFavoriteFolder.is_default) == 1,
                    )
                )
            ).first()
            if row2 is not None:
                return row2.folder_id
            raise

    @staticmethod
    async def create_folder(
        session: AsyncSession,
        mid: int,
        name: str,
        description: str | None = None,
        cover_url: str | None = None,
    ) -> tuple[int, str | None]:
        """创建收藏夹，返回 (folder_id, cover_audit_status)。

        `cover_url` 非空时（2.28.0 起）先下载校验（复用头像校验：http/https、1s 内下载、
        ≤1MB、Content-Type image/*），校验失败抛 ValueError（路由层 422）；校验通过后
        **不直接写入 `cover_url`**，而是插入 `TFolderCoverAudit` pending 记录（先审后发，
        审核通过后才公开封面），`cover_audit_status` 返回 `"pending"`；未传封面时返回 None。
        """
        cover_audit_status: str | None = None
        if cover_url:
            ok, reason = await verify_avatar_url(cover_url, label="封面")
            if not ok:
                raise ValueError(reason)
        folder_id = await generate_moment_id()
        session.add(
            TFavoriteFolder(
                folder_id=folder_id,
                mid=mid,
                name=name.strip() or "未命名收藏夹",
                description=description,
                is_default=0,
            )
        )
        if cover_url:
            # submit 内部 commit（同事务提交收藏夹 + 审核记录）
            await FolderCoverAuditService.submit(
                session, uid=mid, folder_id=folder_id, new_cover=cover_url, old_cover=None
            )
            cover_audit_status = "pending"
        else:
            await session.commit()
        return folder_id, cover_audit_status

    @staticmethod
    async def update_folder(
        session: AsyncSession,
        mid: int,
        folder_id: int,
        name: str | None = None,
        description: str | None = None,
        cover_url: str | None = None,
    ) -> str | None:
        """更新收藏夹（名称/描述/封面；仅本人可改）。返回 cover_audit_status。

        `cover_url` 语义（2.28.0 起）：
        - **非空 URL**：先下载校验（复用头像校验，失败 ValueError），再进入封面审核
          （插入 `TFolderCoverAudit` pending，不直接写 `cover_url`），返回 `"pending"`；
        - **空字符串**：清除封面（直接清 `cover_url`，不经审核，无内容风险）；
        - **None（未传）**：不修改封面。
        """
        row = (
            await session.exec(
                select(TFavoriteFolder).where(
                    col(TFavoriteFolder.folder_id) == folder_id,
                    col(TFavoriteFolder.mid) == mid,
                )
            )
        ).first()
        if row is None:
            raise ValueError("收藏夹不存在")
        if name is not None:
            row.name = name.strip() or "未命名收藏夹"
        if description is not None:
            row.description = description or None
        cover_audit_status: str | None = None
        if cover_url:
            ok, reason = await verify_avatar_url(cover_url, label="封面")
            if not ok:
                raise ValueError(reason)
            # 进入封面审核（pending），不直接写公开封面
            await FolderCoverAuditService.submit(
                session,
                uid=mid,
                folder_id=folder_id,
                new_cover=cover_url,
                old_cover=row.cover_url,
            )
            cover_audit_status = "pending"
        elif cover_url == "":
            # 空串清除封面：直接清公开封面（不经审核）
            row.cover_url = None
            session.add(row)
            await session.commit()
        else:
            session.add(row)
            await session.commit()
        return cover_audit_status

    @staticmethod
    async def delete_folder(
        session: AsyncSession, mid: int, folder_id: int
    ) -> None:
        """删除收藏夹（连同其下收藏明细）。

        删除后若某动态在该用户已无任何收藏夹收录，则 favoriteCount 回退（用户去重）。
        """
        row = (
            await session.exec(
                select(TFavoriteFolder).where(
                    col(TFavoriteFolder.folder_id) == folder_id,
                    col(TFavoriteFolder.mid) == mid,
                )
            )
        ).first()
        if row is None:
            raise ValueError("收藏夹不存在")

        # 删除收藏夹下所有收藏明细，同时按用户去重回退 favoriteCount
        detail_rows = (
            await session.exec(
                select(TMomentFavorite).where(col(TMomentFavorite.folderId) == folder_id)
            )
        ).all()
        affected: list[tuple[InteractionBizTypeEnum, int]] = []
        for detail in detail_rows:
            # 该用户是否还在其它夹收藏同一资源？
            other = (
                await session.exec(
                    select(TMomentFavorite.pk).where(
                        col(TMomentFavorite.mid) == mid,
                        col(TMomentFavorite.bizType) == detail.bizType,
                        col(TMomentFavorite.bizId) == detail.bizId,
                        col(TMomentFavorite.folderId) != folder_id,
                    )
                )
            ).first()
            if other is None:
                affected.append((detail.bizType, detail.bizId))
        await session.exec(delete(TMomentFavorite).where(col(TMomentFavorite.folderId) == folder_id))
        await session.exec(delete(TFavoriteFolder).where(col(TFavoriteFolder.folder_id) == folder_id))
        for biz_type, biz_id in affected:
            if InteractionStatService.is_dynamic(biz_type):
                await MomentStatService.decr_stat(session, biz_id, "favoriteCount", floor_zero=True)
            else:
                await InteractionStatService.decr(session, biz_type, biz_id, "favoriteCount")
        await session.commit()

    @staticmethod
    async def list_folders(session: AsyncSession, mid: int) -> list[dict]:
        """列出我的收藏夹（含各夹收藏数，倒序；2.28.0 起每项含 coverAuditStatus）。"""
        rows = (
            await session.exec(
                select(
                    TFavoriteFolder,
                    func.count(TMomentFavorite.pk).label("favoriteCount"),
                )
                .outerjoin(
                    TMomentFavorite,
                    col(TMomentFavorite.folderId) == col(TFavoriteFolder.folder_id),
                )
                .where(col(TFavoriteFolder.mid) == mid)
                .group_by(TFavoriteFolder.folder_id)
                .order_by(col(TFavoriteFolder.is_default).desc(), col(TFavoriteFolder.folder_id).desc())
            )
        ).all()
        # 批量回填该用户全部夹的 pending 封面审核标记（一次 IN，无 N+1）
        folder_ids = [row[0].folder_id for row in rows]
        pending_folder_ids: set[int] = set()
        if folder_ids:
            pending_rows = (
                await session.exec(
                    select(TFolderCoverAudit.folderId).where(
                        col(TFolderCoverAudit.mid) == mid,
                        col(TFolderCoverAudit.auditStatus) == FolderCoverAuditStatusEnum.PENDING,
                        col(TFolderCoverAudit.folderId).in_(folder_ids),
                    )
                )
            ).all()
            pending_folder_ids = {int(x) for x in pending_rows}
        return [
            {
                "folderId": str(row[0].folder_id),
                "name": row[0].name,
                "description": row[0].description,
                "coverUrl": row[0].cover_url,
                "isDefault": bool(row[0].is_default),
                "favoriteCount": int(row[1] or 0),
                "coverAuditStatus": "pending" if row[0].folder_id in pending_folder_ids else None,
            }
            for row in rows
        ]

    # ==================== 收藏 / 取消 ====================

    @staticmethod
    async def favorite(
        session: AsyncSession,
        mid: int,
        biz_type: InteractionBizTypeEnum | str,
        biz_id: int,
        folder_id: int | str | None = None,
    ) -> bool:
        """收藏资源到指定收藏夹（幂等）。

        Args:
            biz_type: 资源类型（dynamic / lottery / rpa_action / rpa_workflow / rpa_browser）。
            biz_id: 资源 id（动态时 = dynId）。
            folder_id: 目标收藏夹；缺省/None 时自动使用（创建）该用户的默认收藏夹（计划书决策 15），
                保证调用方无需先建夹即可收藏。

        Returns:
            True=本次新增收藏（计数可能 +1）；False=已在同夹收藏过。

        Raises:
            ValueError: 资源不存在 / 收藏夹不存在 / 资源类型非法。
        """
        biz_type = InteractionBizTypeEnum.from_text(biz_type)
        # 校验资源存在
        await InteractionResourceValidator.validate(session, biz_type, biz_id)
        # 未指定收藏夹：自动使用（创建）默认收藏夹（计划书决策 15）
        if folder_id is None:
            folder_id = await FavoriteService.ensure_default_folder(session, mid)
        folder_id = int(folder_id)
        # 收藏夹归属校验
        folder = (
            await session.exec(
                select(TFavoriteFolder).where(
                    col(TFavoriteFolder.folder_id) == folder_id,
                    col(TFavoriteFolder.mid) == mid,
                )
            )
        ).one_or_none()
        if folder is None:
            raise ValueError("收藏夹不存在")

        exists = (
            await session.exec(
                select(TMomentFavorite.pk).where(
                    col(TMomentFavorite.bizType) == biz_type,
                    col(TMomentFavorite.bizId) == biz_id,
                    col(TMomentFavorite.folderId) == folder_id,
                )
            )
        ).first()
        if exists is not None:
            await session.commit()
            return False

        # 动态资源时冗余写入 dynId（bizId=dynId）；非动态 dynId=NULL
        dyn_id = biz_id if InteractionStatService.is_dynamic(biz_type) else None
        session.add(
            TMomentFavorite(
                bizType=biz_type,
                bizId=biz_id,
                dynId=dyn_id,
                folderId=folder_id,
                mid=mid,
            )
        )
        await session.flush()

        # 该用户是否已在其它夹收藏过同一资源？没有才计数 +1（用户去重）
        already = (
            await session.exec(
                select(TMomentFavorite.pk).where(
                    col(TMomentFavorite.mid) == mid,
                    col(TMomentFavorite.bizType) == biz_type,
                    col(TMomentFavorite.bizId) == biz_id,
                    col(TMomentFavorite.folderId) != folder_id,
                )
            )
        ).first()
        if already is None:
            if InteractionStatService.is_dynamic(biz_type):
                await MomentStatService.incr_stat(session, biz_id, "favoriteCount", +1)
            else:
                await InteractionStatService.incr(session, biz_type, biz_id, "favoriteCount", +1)
        await session.commit()
        return True

    @staticmethod
    async def unfavorite(
        session: AsyncSession,
        mid: int,
        biz_type: InteractionBizTypeEnum | str,
        biz_id: int,
        folder_id: int,
    ) -> bool:
        """从指定收藏夹取消收藏（幂等）。

        Returns:
            True=本次删除了收藏（可能计数 -1）；False=原本未收藏。
        """
        biz_type = InteractionBizTypeEnum.from_text(biz_type)
        row = (
            await session.exec(
                select(TMomentFavorite).where(
                    col(TMomentFavorite.bizType) == biz_type,
                    col(TMomentFavorite.bizId) == biz_id,
                    col(TMomentFavorite.folderId) == folder_id,
                    col(TMomentFavorite.mid) == mid,
                )
            )
        ).first()
        if row is None:
            await session.commit()
            return False
        await session.exec(
            delete(TMomentFavorite).where(
                col(TMomentFavorite.bizType) == biz_type,
                col(TMomentFavorite.bizId) == biz_id,
                col(TMomentFavorite.folderId) == folder_id,
                col(TMomentFavorite.mid) == mid,
            )
        )
        # 该用户是否还在其它夹收藏同一资源？没有才计数 -1
        other = (
            await session.exec(
                select(TMomentFavorite.pk).where(
                    col(TMomentFavorite.mid) == mid,
                    col(TMomentFavorite.bizType) == biz_type,
                    col(TMomentFavorite.bizId) == biz_id,
                    col(TMomentFavorite.folderId) != folder_id,
                )
            )
        ).first()
        if other is None:
            if InteractionStatService.is_dynamic(biz_type):
                await MomentStatService.decr_stat(session, biz_id, "favoriteCount", floor_zero=True)
            else:
                await InteractionStatService.decr(session, biz_type, biz_id, "favoriteCount")
        await session.commit()
        return True

    @staticmethod
    async def list_favorite_items(
        session: AsyncSession,
        mid: int,
        folder_id: int,
        page: int,
        page_size: int,
        biz_type: InteractionBizTypeEnum | str | None = None,
    ) -> tuple[int, list[dict]]:
        """某收藏夹下的资源列表（按收藏时间倒序，分页；可指定 bizType 过滤）。

        Returns:
            (total, items)：total 为该夹收藏总数（供分页），items 为当前页资源
            `{"bizType": str, "bizId": str}` 列表（前端按类型渲染）。
        """
        where = [
            col(TMomentFavorite.mid) == mid,
            col(TMomentFavorite.folderId) == folder_id,
        ]
        if biz_type is not None:
            bt = InteractionBizTypeEnum.from_text(biz_type)
            where.append(col(TMomentFavorite.bizType) == bt)
        total = (
            await session.exec(
                select(func.count()).select_from(TMomentFavorite).where(*where)
            )
        ).one()
        rows = (
            await session.exec(
                select(TMomentFavorite.bizType, TMomentFavorite.bizId)
                .where(*where)
                .order_by(col(TMomentFavorite.createdAt).desc())
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
        ).all()
        items = [{"bizType": r[0].to_text(), "bizId": str(r[1])} for r in rows]
        return int(total), items

    @staticmethod
    async def folders_containing_resource(
        session: AsyncSession,
        mid: int,
        biz_type: InteractionBizTypeEnum | str,
        biz_id: int,
    ) -> list[str]:
        """某资源被当前用户收藏在哪些收藏夹（返回 folder_id 字符串列表）。"""
        bt = InteractionBizTypeEnum.from_text(biz_type)
        rows = (
            await session.exec(
                select(TMomentFavorite.folderId).where(
                    col(TMomentFavorite.mid) == mid,
                    col(TMomentFavorite.bizType) == bt,
                    col(TMomentFavorite.bizId) == biz_id,
                )
            )
        ).all()
        return [str(x) for x in rows]

    # ==================== 主页收藏可见性设置 ====================

    @staticmethod
    async def get_setting(session: AsyncSession, mid: int) -> bool:
        """获取主页是否显示收藏（默认 True）。"""
        row = (
            await session.exec(
                select(TUserFavoriteSetting).where(col(TUserFavoriteSetting.mid) == mid)
            )
        ).one_or_none()
        return bool(row.showFavorites) if row else True

    @staticmethod
    async def set_setting(session: AsyncSession, mid: int, show_favorites: bool) -> bool:
        """设置主页是否显示收藏（get_or_create）。返回设置后值。"""
        row = (
            await session.exec(
                select(TUserFavoriteSetting).where(col(TUserFavoriteSetting.mid) == mid)
            )
        ).one_or_none()
        if row is None:
            session.add(
                TUserFavoriteSetting(mid=mid, showFavorites=1 if show_favorites else 0)
            )
        else:
            row.showFavorites = 1 if show_favorites else 0
            session.add(row)
        await session.commit()
        return show_favorites

    # ==================== 他人主页公开读（P4-T11）====================

    @staticmethod
    async def is_public(session: AsyncSession, mid: int) -> bool:
        """该用户主页是否公开展示收藏（showFavorites=1；缺省默认公开）。"""
        row = (
            await session.exec(
                select(TUserFavoriteSetting).where(col(TUserFavoriteSetting.mid) == mid)
            )
        ).one_or_none()
        return bool(row.showFavorites) if row else True

    @staticmethod
    async def list_public_folders(session: AsyncSession, mid: int) -> list[dict] | None:
        """访客查看某用户主页公开的收藏夹列表（含各夹收藏数，倒序）。

        该用户 `showFavorites=0` 时不公开，返回 None（前端隐藏收藏 tab）。
        """
        if not await FavoriteService.is_public(session, mid):
            return None
        return await FavoriteService.list_folders(session, mid)

    @staticmethod
    async def list_public_items(
        session: AsyncSession, mid: int, folder_id: int, page: int, page_size: int
    ) -> tuple[int, list[dict]] | None:
        """访客查看某用户某收藏夹下的资源列表（分页）。

        校验：该收藏夹属于该用户，且该用户 `showFavorites=1`（公开）。
        不公开或夹不属于该用户时返回 None。
        """
        if not await FavoriteService.is_public(session, mid):
            return None
        folder = (
            await session.exec(
                select(TFavoriteFolder.folder_id).where(
                    col(TFavoriteFolder.folder_id) == folder_id,
                    col(TFavoriteFolder.mid) == mid,
                )
            )
        ).first()
        if folder is None:
            return None
        total = (
            await session.exec(
                select(func.count()).select_from(TMomentFavorite).where(
                    col(TMomentFavorite.mid) == mid,
                    col(TMomentFavorite.folderId) == folder_id,
                )
            )
        ).one()
        rows = (
            await session.exec(
                select(TMomentFavorite.bizType, TMomentFavorite.bizId)
                .where(
                    col(TMomentFavorite.mid) == mid,
                    col(TMomentFavorite.folderId) == folder_id,
                )
                .order_by(col(TMomentFavorite.createdAt).desc())
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
        ).all()
        items = [{"bizType": r[0].to_text(), "bizId": str(r[1])} for r in rows]
        return int(total), items


__all__ = ["FavoriteService"]
