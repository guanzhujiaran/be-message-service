"""收藏夹管理操作（2.47.0 对象化；收藏的「文件夹」聚合操作集）。

收藏夹是**用户维度**（不绑定具体 `biz_type`）的容器，管理操作（创建 / 更新 / 删除 /
列表 / 收藏明细 / 主页可见性设置 / 他人公开读）从原静态 `FavoriteService` 迁移为
类对象：接口层直接实例化 `FavoriteFolderAction(session, actor_mid)` 后调用方法。

收藏夹业务约束（计划书 §4.2）：

- **多夹 + 默认夹**：一个用户可有多个收藏夹，且每用户有且仅有一个默认收藏夹
  （`is_default=1`）；新用户首次进入收藏流程时由 `ensure_default` 自动创建。
- **收藏夹字段**：自定义封面（`cover_url`，**仅存 URL 不转存图片**）、名称、描述；
  封面「先审后发」（`TFolderCoverAudit` pending → 审核通过才公开）。
- **收藏明细**：`TMomentFavorite` 唯一约束 `(bizType, bizId, folderId)` 保证同一夹内
  同一资源不重复收藏；同一资源可被收藏到多个不同夹。
- **favoriteCount 语义（按用户去重）**：用户首次收藏某资源（其它夹也未曾收藏过）才
  `+1`；仅当该用户在所有夹都不再收藏该资源时才 `-1`。
- **主页收藏可见性**：`TUserFavoriteSetting` 每用户一行，`showFavorites` 默认 1。
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
from app.services.user.avatar_check import verify_avatar_url
from app.services.user.folder_cover_audit import FolderCoverAuditService
from app.services.moment.interaction import (
    BeMessageInteractionStatService as InteractionStatService,
)
from app.services.moment.moment_stat import MomentStatService


class FavoriteFolderAction:
    """收藏夹管理操作（对象化；持有 session + 操作者 mid）。

    用法：``await FavoriteFolderAction(session, user.mid).list_folders()`` 等。
    他人主页公开读（``list_public_folders`` / ``list_public_items``）以目标 mid 为参数，
    操作者为访客视角。
    """

    def __init__(self, session: AsyncSession, actor_mid: int) -> None:
        self.session = session
        self.actor_mid = int(actor_mid)

    # ==================== 默认收藏夹 ====================

    async def ensure_default(self) -> int:
        """确保默认收藏夹存在，返回其 folder_id（get_or_create，并发安全）。

        首次进入收藏流程（创建/列表/收藏）时调用；每用户仅一个默认夹。
        """
        session = self.session
        mid = self.actor_mid
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
        except Exception:
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

    # ==================== 收藏夹 CRUD ====================

    async def create(
        self,
        name: str,
        description: str | None = None,
        cover_url: str | None = None,
    ) -> tuple[int, str | None]:
        """创建收藏夹，返回 (folder_id, cover_audit_status)。

        `cover_url` 非空时先下载校验（复用头像校验：http/https、1s 内下载、
        ≤1MB、Content-Type image/*），校验失败抛 ValueError（路由层 422）；校验通过后
        **不直接写入 `cover_url`**，而是插入 `TFolderCoverAudit` pending 记录（先审后发），
        `cover_audit_status` 返回 `"pending"`；未传封面时返回 None。
        """
        session = self.session
        cover_audit_status: str | None = None
        if cover_url:
            ok, reason = await verify_avatar_url(cover_url, label="封面")
            if not ok:
                raise ValueError(reason)
        folder_id = await generate_moment_id()
        session.add(
            TFavoriteFolder(
                folder_id=folder_id,
                mid=self.actor_mid,
                name=name.strip() or "未命名收藏夹",
                description=description,
                is_default=0,
            )
        )
        if cover_url:
            # submit 内部 commit（同事务提交收藏夹 + 审核记录）
            await FolderCoverAuditService.submit(
                session,
                uid=self.actor_mid,
                folder_id=folder_id,
                new_cover=cover_url,
                old_cover=None,
            )
            cover_audit_status = "pending"
        else:
            await session.commit()
        return folder_id, cover_audit_status

    async def update(
        self,
        folder_id: int,
        name: str | None = None,
        description: str | None = None,
        cover_url: str | None = None,
    ) -> str | None:
        """更新收藏夹（名称/描述/封面；仅本人可改）。返回 cover_audit_status。

        `cover_url` 语义：
        - **非空 URL**：先下载校验（失败 ValueError），再进入封面审核
          （插入 pending，不直接写 `cover_url`），返回 `"pending"`；
        - **空字符串**：清除封面（直接清 `cover_url`，不经审核，无内容风险）；
        - **None（未传）**：不修改封面。
        """
        session = self.session
        row = (
            await session.exec(
                select(TFavoriteFolder).where(
                    col(TFavoriteFolder.folder_id) == folder_id,
                    col(TFavoriteFolder.mid) == self.actor_mid,
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
                uid=self.actor_mid,
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

    async def delete(self, folder_id: int) -> None:
        """删除收藏夹（连同其下收藏明细）。

        删除后若某资源在该用户已无任何收藏夹收录，则 favoriteCount 回退（用户去重）。
        """
        session = self.session
        mid = self.actor_mid
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
                select(TMomentFavorite).where(
                    col(TMomentFavorite.folderId) == folder_id
                )
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
        await session.exec(
            delete(TMomentFavorite).where(col(TMomentFavorite.folderId) == folder_id)
        )
        await session.exec(
            delete(TFavoriteFolder).where(col(TFavoriteFolder.folder_id) == folder_id)
        )
        for biz_type, biz_id in affected:
            if InteractionStatService.is_dynamic(biz_type):
                await MomentStatService.decr_stat(
                    session, biz_id, "favoriteCount", floor_zero=True
                )
            else:
                await InteractionStatService.decr(
                    session, biz_type, biz_id, "favoriteCount"
                )
        await session.commit()

    async def list_folders(self) -> list[dict]:
        """列出我的收藏夹（含各夹收藏数，倒序；每项含 coverAuditStatus）。"""
        session = self.session
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
                .where(col(TFavoriteFolder.mid) == self.actor_mid)
                .group_by(TFavoriteFolder.folder_id)
                .order_by(
                    col(TFavoriteFolder.is_default).desc(),
                    col(TFavoriteFolder.folder_id).desc(),
                )
            )
        ).all()
        # 批量回填该用户全部夹的 pending 封面审核标记（一次 IN，无 N+1）
        folder_ids = [row[0].folder_id for row in rows]
        pending_folder_ids: set[int] = set()
        if folder_ids:
            pending_rows = (
                await session.exec(
                    select(TFolderCoverAudit.folderId).where(
                        col(TFolderCoverAudit.mid) == self.actor_mid,
                        col(TFolderCoverAudit.auditStatus)
                        == FolderCoverAuditStatusEnum.PENDING,
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
                "coverAuditStatus": (
                    "pending" if row[0].folder_id in pending_folder_ids else None
                ),
            }
            for row in rows
        ]

    # ==================== 收藏明细 ====================

    async def list_items(
        self,
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
        session = self.session
        where = [
            col(TMomentFavorite.mid) == self.actor_mid,
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
        items = [{"bizType": r[0], "bizId": str(r[1])} for r in rows]
        return int(total), items

    async def folders_containing(
        self, biz_type: InteractionBizTypeEnum | str, biz_id: int
    ) -> list[str]:
        """某资源被当前用户收藏在哪些收藏夹（返回 folder_id 字符串列表）。"""
        bt = InteractionBizTypeEnum.from_text(biz_type)
        rows = (
            await self.session.exec(
                select(TMomentFavorite.folderId).where(
                    col(TMomentFavorite.mid) == self.actor_mid,
                    col(TMomentFavorite.bizType) == bt,
                    col(TMomentFavorite.bizId) == biz_id,
                )
            )
        ).all()
        return [str(x) for x in rows]

    # ==================== 主页收藏可见性设置 ====================

    async def get_setting(self) -> bool:
        """获取主页是否显示收藏（默认 True）。"""
        row = (
            await self.session.exec(
                select(TUserFavoriteSetting).where(
                    col(TUserFavoriteSetting.mid) == self.actor_mid
                )
            )
        ).one_or_none()
        return bool(row.showFavorites) if row else True

    async def set_setting(self, show_favorites: bool) -> bool:
        """设置主页是否显示收藏（get_or_create）。返回设置后值。"""
        session = self.session
        row = (
            await session.exec(
                select(TUserFavoriteSetting).where(
                    col(TUserFavoriteSetting.mid) == self.actor_mid
                )
            )
        ).one_or_none()
        if row is None:
            session.add(
                TUserFavoriteSetting(
                    mid=self.actor_mid, showFavorites=1 if show_favorites else 0
                )
            )
        else:
            row.showFavorites = 1 if show_favorites else 0
            session.add(row)
        await session.commit()
        return show_favorites

    # ==================== 他人主页公开读（P4-T11）====================

    async def list_public_folders(self, target_mid: int) -> list[dict] | None:
        """访客查看某用户主页公开的收藏夹列表（含各夹收藏数，倒序）。

        该用户 `showFavorites=0` 时不公开，返回 None（前端隐藏收藏 tab）。
        """
        if not await self.is_public(target_mid):
            return None
        return await self._list_folders_of(target_mid)

    async def list_public_items(
        self, target_mid: int, folder_id: int, page: int, page_size: int
    ) -> tuple[int, list[dict]] | None:
        """访客查看某用户某收藏夹下的资源列表（分页）。

        校验：该收藏夹属于该用户，且该用户 `showFavorites=1`（公开）。
        不公开或夹不属于该用户时返回 None。
        """
        session = self.session
        if not await self.is_public(target_mid):
            return None
        folder = (
            await session.exec(
                select(TFavoriteFolder.folder_id).where(
                    col(TFavoriteFolder.folder_id) == folder_id,
                    col(TFavoriteFolder.mid) == target_mid,
                )
            )
        ).first()
        if folder is None:
            return None
        total = (
            await session.exec(
                select(func.count())
                .select_from(TMomentFavorite)
                .where(
                    col(TMomentFavorite.mid) == target_mid,
                    col(TMomentFavorite.folderId) == folder_id,
                )
            )
        ).one()
        rows = (
            await session.exec(
                select(TMomentFavorite.bizType, TMomentFavorite.bizId)
                .where(
                    col(TMomentFavorite.mid) == target_mid,
                    col(TMomentFavorite.folderId) == folder_id,
                )
                .order_by(col(TMomentFavorite.createdAt).desc())
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
        ).all()
        items = [{"bizType": r[0], "bizId": str(r[1])} for r in rows]
        return int(total), items

    # ==================== 内部辅助 ====================

    async def is_public(self, target_mid: int) -> bool:
        """该用户主页是否公开展示收藏（showFavorites=1；缺省默认公开）。"""
        row = (
            await self.session.exec(
                select(TUserFavoriteSetting).where(
                    col(TUserFavoriteSetting.mid) == target_mid
                )
            )
        ).one_or_none()
        return bool(row.showFavorites) if row else True

    async def _list_folders_of(self, target_mid: int) -> list[dict]:
        """列出指定用户（含本人）的收藏夹（复用 list_folders 查询，替换持有者 mid）。"""
        saved = self.actor_mid
        self.actor_mid = int(target_mid)
        try:
            return await self.list_folders()
        finally:
            self.actor_mid = saved


__all__ = ["FavoriteFolderAction"]
