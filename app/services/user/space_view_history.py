"""空间浏览历史服务（计划书 §3.4）。

数据源 `TInteractionViewLog`（每用户每资源一行合并）：只存 `viewCount` + `lastViewAt`，
同一资源多次访问会合并成一条，本期按这个精度做。资源标题回查：

- dynamic（`bizType=DYNAMIC`）→ `TMoment.contentText`；
- 其余 bizType（lottery / rpa_* / user）本地无主表，标题留 `None`，待 P3 统一存在性回查。

浏览历史**仅本人可读**（路由层校验 `mid == viewer`），不做隐私过滤。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col

from app.models.db import TMoment, TInteractionViewLog
from app.models.schemas.space import SpaceViewHistoryItem, SpaceViewHistoryResp
from bili_common.models import InteractionBizTypeEnum

DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE = 50


def _parse_cursor(cursor: str | None) -> datetime | None:
    """游标 ISO 时间 → datetime（非法 / 空返回 None = 第一页）。"""
    if not cursor:
        return None
    try:
        return datetime.fromisoformat(cursor)
    except (TypeError, ValueError):
        return None


class SpaceViewHistoryService:
    """空间浏览历史（静态方法集合）。"""

    @staticmethod
    async def list(
        session: AsyncSession,
        mid: int,
        cursor: str | None = None,
        page_size: int = DEFAULT_PAGE_SIZE,
    ) -> SpaceViewHistoryResp:
        """按 `last_view_at` 倒序取某用户的浏览历史（每用户每资源一行）。"""
        page_size = max(1, min(int(page_size), MAX_PAGE_SIZE))
        cursor_dt = _parse_cursor(cursor)

        stmt = (
            select(TInteractionViewLog)
            .where(col(TInteractionViewLog.mid) == int(mid))
            .order_by(col(TInteractionViewLog.lastViewAt).desc())
        )
        if cursor_dt is not None:
            stmt = stmt.where(col(TInteractionViewLog.lastViewAt) < cursor_dt)
        rows = (await session.exec(stmt.limit(page_size + 1))).all()
        has_more = len(rows) > page_size
        rows = rows[:page_size]

        # 动态标题回查；其余 bizType 留 None（待 P3）
        dyn_ids = [
            int(r.bizId)
            for r in rows
            if r.bizType == InteractionBizTypeEnum.DYNAMIC and r.bizId
        ]
        titles: dict[int, str] = {}
        if dyn_ids:
            mrows = (
                await session.exec(
                    select(col(TMoment.dynId), col(TMoment.contentText)).where(
                        col(TMoment.dynId).in_(dyn_ids)
                    )
                )
            ).all()
            titles = {int(d): (t or "") for d, t in mrows}

        items = []
        for r in rows:
            biz_id = int(r.bizId)
            title = (
                titles.get(biz_id)
                if r.bizType == InteractionBizTypeEnum.DYNAMIC
                else None
            )
            items.append(
                SpaceViewHistoryItem(
                    biz_type=r.bizType.to_text(),
                    biz_id=biz_id,
                    title=title,
                    last_view_at=r.lastViewAt,
                    view_count=int(r.viewCount),
                )
            )
        next_cursor = rows[-1].lastViewAt.isoformat() if has_more and rows else None
        return SpaceViewHistoryResp(items=items, has_more=has_more, cursor=next_cursor)


__all__ = ["SpaceViewHistoryService"]
