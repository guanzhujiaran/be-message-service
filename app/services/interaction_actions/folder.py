"""收藏夹管理操作（2.47.0 对象化；收藏的「文件夹」聚合操作集）。

收藏夹是**用户维度**（不绑定具体 `biz_type`）的容器，管理操作（创建 / 更新 / 删除 /
列表 / 收藏明细 / 他人公开读）从原静态 `FavoriteService` 迁移为
类对象：接口层直接实例化 `FavoriteFolderAction(session, actor_mid)` 后调用方法。

收藏夹业务约束（计划书 §4.2）：

- **多夹 + 默认夹**：一个用户可有多个收藏夹，且每用户有且仅有一个默认收藏夹
  （`is_default=1`）；新用户首次进入收藏流程时由 `ensure_default` 自动创建。
- **收藏夹字段**：自定义封面（`cover_url`，**仅存 URL 不转存图片**）、名称、描述；
  封面「先审后发」（`TFolderCoverAudit` pending → 审核通过才公开）。
- **收藏明细**：`TResourceFavorite` 唯一约束 `(bizType, bizId, folderId)` 保证同一夹内
  同一资源不重复收藏；同一资源可被收藏到多个不同夹。
- **favoriteCount 语义（按用户去重）**：用户首次收藏某资源（其它夹也未曾收藏过）才
  `+1`；仅当该用户在所有夹都不再收藏该资源时才 `-1`。
- **收藏夹可见性**：`TFavoriteFolder.is_public` 逐夹控制（默认公开），他人视角只读公开夹；
  2026-10-08 前的全局开关 `TUserFavoriteSetting` 已删除。
"""

from bili_common.models import InteractionBizTypeEnum
from sqlalchemy import func
from sqlmodel import col, delete, select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.core.sharding import generate_moment_id
from app.models.db import (
    TFavoriteFolder,
    TFolderCoverAudit,
    TResourceFavorite,
)
from app.models.enums import ResourceAuditStatusEnum
from app.services.moment.interaction import (
    BeMessageInteractionStatService as InteractionStatService,
)
from app.services.moment.moment_stat import MomentStatService
from app.services.user.avatar_check import verify_avatar_url
from app.services.user.folder_cover_audit import FolderCoverAuditService


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
        is_public: bool = True,
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
        final_name = name.strip() or "未命名收藏夹"
        # 内容审核：收藏夹名称 / 描述（命中违规即拒绝，作者即本人，无跨人通知）
        from app.services.audit import audit_text

        name_res = audit_text(final_name, check_link=False)
        if name_res.rejected:
            raise ValueError(f"收藏夹名称未通过审核：{name_res.reason}")
        if description:
            desc_res = audit_text(description)
            if desc_res.rejected:
                raise ValueError(f"收藏夹描述未通过审核：{desc_res.reason}")
        session.add(
            TFavoriteFolder(
                folder_id=folder_id,
                mid=self.actor_mid,
                name=final_name,
                description=description,
                is_default=0,
                is_public=bool(is_public),
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
        is_public: bool | None = None,
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
        # 内容审核：更新的名称 / 描述（命中违规即拒绝）
        from app.services.audit import audit_text

        if name is not None:
            final_name = name.strip() or "未命名收藏夹"
            name_res = audit_text(final_name, check_link=False)
            if name_res.rejected:
                raise ValueError(f"收藏夹名称未通过审核：{name_res.reason}")
            row.name = final_name
        if description is not None:
            if description:
                desc_res = audit_text(description)
                if desc_res.rejected:
                    raise ValueError(f"收藏夹描述未通过审核：{desc_res.reason}")
            row.description = description or None
        if is_public is not None:
            row.is_public = bool(is_public)
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
                select(TResourceFavorite).where(
                    col(TResourceFavorite.folderId) == folder_id
                )
            )
        ).all()
        affected: list[tuple[InteractionBizTypeEnum, int]] = []
        for detail in detail_rows:
            # 该用户是否还在其它夹收藏同一资源？
            other = (
                await session.exec(
                    select(TResourceFavorite.pk).where(
                        col(TResourceFavorite.mid) == mid,
                        col(TResourceFavorite.bizType) == detail.bizType,
                        col(TResourceFavorite.bizId) == detail.bizId,
                        col(TResourceFavorite.folderId) != folder_id,
                    )
                )
            ).first()
            if other is None:
                affected.append((detail.bizType, detail.bizId))
        await session.exec(
            delete(TResourceFavorite).where(
                col(TResourceFavorite.folderId) == folder_id
            )
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

    async def list_folders(self, only_public: bool = False) -> list[dict]:
        """列出我的收藏夹（含各夹收藏数，倒序；每项含 coverAuditStatus）。

        `only_public=True` 时只返回**公开**的收藏夹（他人视角用；本人视角传 False 看全部）。
        """
        session = self.session
        conditions = [col(TFavoriteFolder.mid) == self.actor_mid]
        if only_public:
            conditions.append(col(TFavoriteFolder.is_public).is_(True))
        rows = (
            await session.exec(
                select(
                    TFavoriteFolder,
                    func.count(TResourceFavorite.pk).label("favoriteCount"),
                )
                .outerjoin(
                    TResourceFavorite,
                    col(TResourceFavorite.folderId) == col(TFavoriteFolder.folder_id),
                )
                .where(*conditions)
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
                        == ResourceAuditStatusEnum.AUDITING,
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
                "isPublic": bool(row[0].is_public),
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
            col(TResourceFavorite.mid) == self.actor_mid,
            col(TResourceFavorite.folderId) == folder_id,
        ]
        if biz_type is not None:
            bt = InteractionBizTypeEnum.from_text(biz_type)
            where.append(col(TResourceFavorite.bizType) == bt)
        total = (
            await session.exec(
                select(func.count()).select_from(TResourceFavorite).where(*where)
            )
        ).one()
        rows = (
            await session.exec(
                select(TResourceFavorite.bizType, TResourceFavorite.bizId)
                .where(*where)
                .order_by(col(TResourceFavorite.created_at).desc())
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
        ).all()
        items = [{"bizType": r[0], "bizId": str(r[1])} for r in rows]
        return int(total), items

    async def folders_containing(
        self, biz_type: InteractionBizTypeEnum | str, biz_id: str
    ) -> list[str]:
        """某资源被当前用户收藏在哪些收藏夹（返回 folder_id 字符串列表）。"""
        bt = InteractionBizTypeEnum.from_text(biz_type)
        rows = (
            await self.session.exec(
                select(TResourceFavorite.folderId).where(
                    col(TResourceFavorite.mid) == self.actor_mid,
                    col(TResourceFavorite.bizType) == bt,
                    col(TResourceFavorite.bizId) == biz_id,
                )
            )
        ).all()
        return [str(x) for x in rows]

    # ==================== 他人主页公开读（P4-T11）====================

    async def list_public_folders(self, target_mid: int) -> list[dict]:
        """访客查看某用户主页**公开的**收藏夹列表（含各夹收藏数，倒序）。

        2026-10-08 起不再有全局开关：只返回 `is_public=1` 的夹；
        一个公开夹都没有时返回空数组（前端据此隐藏收藏 tab）。
        """
        return await self._list_folders_of(target_mid, only_public=True)

    async def list_public_items(
        self, target_mid: int, folder_id: int, page: int, page_size: int
    ) -> tuple[int, list[dict]] | None:
        """访客查看某用户某**公开**收藏夹下的资源列表（分页）。

        校验：该收藏夹属于该用户且 `is_public=1`；夹不存在或未公开时返回 None
        （由路由层转成空列表）。
        """
        session = self.session
        folder = (
            await session.exec(
                select(TFavoriteFolder.folder_id).where(
                    col(TFavoriteFolder.folder_id) == folder_id,
                    col(TFavoriteFolder.mid) == target_mid,
                    col(TFavoriteFolder.is_public).is_(True),
                )
            )
        ).first()
        if folder is None:
            return None
        total = (
            await session.exec(
                select(func.count())
                .select_from(TResourceFavorite)
                .where(
                    col(TResourceFavorite.mid) == target_mid,
                    col(TResourceFavorite.folderId) == folder_id,
                )
            )
        ).one()
        rows = (
            await session.exec(
                select(TResourceFavorite.bizType, TResourceFavorite.bizId)
                .where(
                    col(TResourceFavorite.mid) == target_mid,
                    col(TResourceFavorite.folderId) == folder_id,
                )
                .order_by(col(TResourceFavorite.created_at).desc())
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
        ).all()
        items = [{"bizType": r[0], "bizId": str(r[1])} for r in rows]
        return int(total), items

    # ==================== 内部辅助 ====================

    async def _list_folders_of(
        self, target_mid: int, only_public: bool = False
    ) -> list[dict]:
        """列出指定用户（含本人）的收藏夹（复用 list_folders 查询，替换持有者 mid）。"""
        saved = self.actor_mid
        self.actor_mid = int(target_mid)
        try:
            return await self.list_folders(only_public=only_public)
        finally:
            self.actor_mid = saved


__all__ = ["FavoriteFolderAction"]
