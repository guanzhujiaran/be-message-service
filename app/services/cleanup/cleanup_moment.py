"""动态领域删除（`cleanup_moment`）。

清除指定 uid 用户在 be-message MySQL 的动态相关全部数据：

- **发布的动态** `TMoment WHERE mid=uid`：`TResourceLike` / `TResourceDislike`
  / `TResourceAuditLog`（2.55.0 起，去 dynId 冗余列与 FK）不再有
  `TMoment.dynId` 的 CASCADE 依赖，**改为显式按
  `(bizType=DYNAMIC, bizId IN dyn_ids)` 删除**，与 `TResourceFeed` /
  `TInteractionStat` 清理范式一致；
- **点赞 / 浏览他人动态的痕迹**（`TResourceLike` / `TInteractionViewLog` 的 `mid`）：
  这些行的 `mid` 是该用户（操作者），非动态作者，需单独按 `mid` 删，
  不会随其发布动态级联。

注意：本模块只操作 be-message 主库，调用方传入可用的 `AsyncSession`。
"""

from sqlalchemy import or_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col, delete, select

from app.models.db import (
    TMoment,
    TResourceLike,
    TResourceDislike,
    TResourceAuditLog,
    TInteractionStat,
    TInteractionViewLog,
    TResourceFeed,
)
from bili_common.models import InteractionBizTypeEnum


class CleanupMomentService:
    """动态领域删除（静态方法集合）。"""

    @staticmethod
    async def delete_all_by_uid(session: AsyncSession, uid: int) -> None:
        """删除指定 uid 用户的全部动态数据（调用方负责 commit）。

        按依赖顺序：先清「操作痕迹」（mid=操作者），再删其发布动态
        相关的明细 / 计数 / Feed 元数据（按 bizType+bizId 显式删），
        最后删主表 `TMoment`。
        """
        # 我点赞 / 点踩 / 浏览他人动态的痕迹（mid = 操作者）
        await session.exec(
            delete(TResourceLike).where(col(TResourceLike.mid) == uid)
        )
        await session.exec(
            delete(TResourceDislike).where(col(TResourceDislike.mid) == uid)
        )
        # 2.36.0：浏览去重统一 TInteractionViewLog（按操作者 mid 删痕迹）
        await session.exec(
            delete(TInteractionViewLog).where(col(TInteractionViewLog.mid) == uid)
        )
        # 我发布的动态：先删统一计数/Feed 元数据/审核流水（按 bizType+bizId 显式删）+ 点赞/点踩明细，
        # 最后删主表 TMoment（不再依赖 CASCADE：2.55.0 起子表去 FK）。
        dyn_ids = select(TMoment.dynId).where(col(TMoment.mid) == uid)
        await session.exec(
            delete(TResourceFeed).where(
                col(TResourceFeed.bizType) == InteractionBizTypeEnum.DYNAMIC,
                col(TResourceFeed.bizId).in_(dyn_ids),
            )
        )
        await session.exec(
            delete(TInteractionStat).where(
                col(TInteractionStat.bizType) == InteractionBizTypeEnum.DYNAMIC,
                col(TInteractionStat.bizId).in_(dyn_ids),
            )
        )
        await session.exec(
            delete(TResourceAuditLog).where(
                col(TResourceAuditLog.bizType) == InteractionBizTypeEnum.DYNAMIC,
                col(TResourceAuditLog.bizId).in_(dyn_ids),
            )
        )
        await session.exec(
            delete(TResourceLike).where(
                col(TResourceLike.bizType) == InteractionBizTypeEnum.DYNAMIC,
                col(TResourceLike.bizId).in_(dyn_ids),
            )
        )
        await session.exec(
            delete(TResourceDislike).where(
                col(TResourceDislike.bizType) == InteractionBizTypeEnum.DYNAMIC,
                col(TResourceDislike.bizId).in_(dyn_ids),
            )
        )
        await session.exec(delete(TMoment).where(col(TMoment.mid) == uid))


__all__ = ["CleanupMomentService"]
