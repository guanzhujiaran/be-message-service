"""空间行为时间线服务（计划书 §3.3 / §3.3.1）。

我的主页与 TA 主页**共用**同一接口，按访问者身份做隐私过滤：

- `viewer == target`（我的主页）→ 不过滤，返回全部三类条目；
- `viewer != target` 或未登录（TA 主页）→ 按隐私开关**整类过滤**：
  关注条目受 ``show_follow_list``、点赞受 ``show_like_list``、收藏受 ``showFavorites``，
  关闭的类型不出现在结果里（不是返回空对象）。

数据源三张表都按「操作人 mid + created_at 倒序」命中既有索引，各自取窗口后
在内存合并排序。**反映「当前有效状态」**（取消点赞 / 取关 / 取消收藏的明细行被
物理删除，行存在即有效），并做两处显式处理：

1. ``msg_user_follow`` 拉黑是把同一行原地翻转成 ``BLOCKED``（保留原 ``created_at``），
   必须过滤 ``status = FOLLOWING``，否则会把「被拉黑的人」当「已关注的人」展示；
2. 动态软删不会级联删赞 / 收藏明细，时间线必须 JOIN ``TMoment`` 校验
   ``deletedAt IS NULL AND auditStatus = 'NORMAL'``，避免指向已删动态的残留行。

收藏明细唯一键是 ``(bizType, bizId, folderId)``，同一资源可进多个夹，需按
``(bizId)`` 去重，否则一条动态刷出多条收藏记录。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col

from app.models.db import TMoment, TResourceFavorite, TFavoriteFolder, TResourceLike
from app.models.db.follow_tbl import UserFollow
from app.models.enums import FollowStatusEnum, ResourceAuditStatusEnum
from app.models.schemas.space import SpaceTimelineItem, SpaceTimelineResp
from bili_common.models import InteractionBizTypeEnum

from app.services.user.account import PptrUser
from app.services.user.space_privacy import SpacePrivacyService

DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE = 50
#: 收藏明细按 bizId 去重需少量超取，缓冲倍数（去重后仍有足够条目排序取页）
_FAV_BUFFER = 3


def _parse_cursor(cursor: str | None) -> datetime | None:
    """游标 ISO 时间 → datetime（非法 / 空返回 None = 第一页）。"""
    if not cursor:
        return None
    try:
        return datetime.fromisoformat(cursor)
    except (TypeError, ValueError):
        return None


class SpaceTimelineService:
    """空间行为时间线（静态方法集合）。"""

    @staticmethod
    async def list(
        session: AsyncSession,
        target_mid: int,
        viewer_mid: int | None,
        cursor: str | None = None,
        page_size: int = DEFAULT_PAGE_SIZE,
    ) -> SpaceTimelineResp:
        """按 `acted_at` 倒序聚合关注 / 点赞 / 收藏三类条目，并按 viewer 过滤。

        `viewer_mid` 缺失（未登录）按 TA 主页口径走公开过滤；游标推进用
        「本页最后一条的 acted_at」，逐页重扫当前时间窗口，适合个人空间的时间线。
        """
        page_size = max(1, min(int(page_size), MAX_PAGE_SIZE))
        cursor_dt = _parse_cursor(cursor)

        # 私域过滤：仅他人视角按开关整类过滤；本人 / 未登录取公开集合
        flags = None
        if viewer_mid is not None and int(viewer_mid) != int(target_mid):
            flags = await SpacePrivacyService.get_flags(session, target_mid)
        show_follow = flags is None or flags.show_follow_list
        show_like = flags is None or flags.show_like_list
        show_fav = flags is None or flags.show_favorites

        entries: list[dict] = []

        if show_follow:
            stmt = (
                select(
                    col(UserFollow.target_mid).label("target_mid"),
                    col(UserFollow.created_at).label("acted_at"),
                )
                .where(
                    col(UserFollow.mid) == int(target_mid),
                    col(UserFollow.status) == FollowStatusEnum.FOLLOWING,
                )
                .order_by(col(UserFollow.created_at).desc())
            )
            if cursor_dt is not None:
                stmt = stmt.where(col(UserFollow.created_at) < cursor_dt)
            rows = (await session.exec(stmt.limit(page_size))).all()
            for r in rows:
                entries.append(
                    {
                        "act_type": "follow",
                        "target_mid": int(r.target_mid),
                        "acted_at": r.acted_at,
                    }
                )

        if show_like:
            stmt = (
                select(
                    col(TResourceLike.bizId).label("biz_id"),
                    col(TMoment.mid).label("author_mid"),
                    col(TMoment.contentText).label("content"),
                    col(TResourceLike.created_at).label("acted_at"),
                )
                .join(
                    TMoment,
                    and_(
                        col(TMoment.dynId) == col(TResourceLike.bizId),
                        col(TMoment.deletedAt).is_(None),
                        col(TMoment.auditStatus) == ResourceAuditStatusEnum.NORMAL,
                    ),
                )
                .where(
                    col(TResourceLike.mid) == int(target_mid),
                    col(TResourceLike.bizType) == InteractionBizTypeEnum.DYNAMIC,
                )
                .order_by(col(TResourceLike.created_at).desc())
            )
            if cursor_dt is not None:
                stmt = stmt.where(col(TResourceLike.created_at) < cursor_dt)
            rows = (await session.exec(stmt.limit(page_size))).all()
            for r in rows:
                entries.append(
                    {
                        "act_type": "like",
                        "target_dyn_id": int(r.biz_id),
                        "target_mid": int(r.author_mid),
                        "target_text": r.content,
                        "acted_at": r.acted_at,
                    }
                )

        if show_fav:
            stmt = (
                select(
                    col(TResourceFavorite.bizId).label("biz_id"),
                    col(TMoment.mid).label("author_mid"),
                    col(TMoment.contentText).label("content"),
                    col(TFavoriteFolder.name).label("folder_name"),
                    col(TResourceFavorite.created_at).label("acted_at"),
                )
                .join(
                    TMoment,
                    and_(
                        col(TMoment.dynId) == col(TResourceFavorite.bizId),
                        col(TMoment.deletedAt).is_(None),
                        col(TMoment.auditStatus) == ResourceAuditStatusEnum.NORMAL,
                    ),
                )
                .join(
                    TFavoriteFolder,
                    col(TFavoriteFolder.folder_id) == col(TResourceFavorite.folderId),
                )
                .where(
                    col(TResourceFavorite.mid) == int(target_mid),
                    col(TResourceFavorite.bizType) == InteractionBizTypeEnum.DYNAMIC,
                )
                .order_by(col(TResourceFavorite.created_at).desc())
            )
            if cursor_dt is not None:
                stmt = stmt.where(col(TResourceFavorite.created_at) < cursor_dt)
            rows = (await session.exec(stmt.limit(page_size * _FAV_BUFFER))).all()
            seen_biz: set[int] = set()
            for r in rows:
                biz_id = int(r.biz_id)
                if biz_id in seen_biz:
                    continue
                seen_biz.add(biz_id)
                entries.append(
                    {
                        "act_type": "favorite",
                        "target_dyn_id": biz_id,
                        "target_mid": int(r.author_mid),
                        "target_text": r.content,
                        "target_folder_name": r.folder_name,
                        "acted_at": r.acted_at,
                    }
                )

        entries.sort(key=lambda e: e["acted_at"], reverse=True)
        page = entries[:page_size]
        has_more = len(entries) > page_size
        next_cursor = (
            entries[page_size - 1]["acted_at"].isoformat() if has_more else None
        )

        if not page:
            return SpaceTimelineResp(items=[], has_more=False, cursor=None)

        # 批量回查用户简写（被关注者 + 动态作者），pptr 弱依赖
        mid_set = {int(e["target_mid"]) for e in page}
        briefs = await PptrUser.get_many([m for m in mid_set])

        items = []
        for e in page:
            target_mid = int(e["target_mid"])
            brief = briefs.get(target_mid)
            items.append(
                SpaceTimelineItem(
                    act_type=e["act_type"],
                    target_mid=target_mid,
                    target_name=brief.name if brief else None,
                    target_face=brief.face if brief else None,
                    target_text=e.get("target_text"),
                    target_dyn_id=e.get("target_dyn_id"),
                    target_folder_name=e.get("target_folder_name"),
                    acted_at=e["acted_at"],
                )
            )
        return SpaceTimelineResp(items=items, has_more=has_more, cursor=next_cursor)


__all__ = ["SpaceTimelineService"]
