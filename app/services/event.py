"""事件提醒服务（点赞 / 回复 / @提及）。

三条主线：

1. **上报（write）**：先过消息设置这道闸门 → 计算幂等键 → 落库 →
   按用户活跃度决定实时推送还是进批量队列。
2. **聚合读（read）**：按 `source_type + source_id` 分组，
   把「12 个人赞了同一条动态」收敛成一张卡片，避免消息中心被同质消息刷屏。
   分组走 `idx_event_group` 联合索引，不需要额外的聚合表。
3. **已读管理**：支持按 id、按类型、按聚合分组三种粒度。

幂等：`dedup_key`（唯一索引）由 `mid:event_type:actor_mid:source_type:source_id:biz_id`
摘要而来。MQ 重投、前端重试、爬虫重复扫描都会被数据库直接拦掉，
上层拿到 `duplicated=True` 即可，无需自己去重。
"""

import hashlib
from datetime import datetime

from loguru import logger
from sqlalchemy import case, func, tuple_
from sqlalchemy.exc import IntegrityError
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.db import EventMessage, EventReadCursor
from app.models.enums import EventTypeEnum, SourceTypeEnum
from app.models.schemas import (
    EventActorBrief,
    EventAggregateItem,
    EventItem,
    EventReadReq,
    EventReadResp,
    EventReportReq,
    EventReportResp,
)
from app.services.setting import SettingService

# 每张聚合卡片最多展示的触发者头像数
_MAX_ACTORS_PER_GROUP = 3
# 为了在内存里凑齐每组的头像，单次最多回捞的明细条数（防止大分组撑爆内存）
_ACTOR_SCAN_LIMIT = 500


def build_dedup_key(
    mid: int,
    event_type: EventTypeEnum,
    actor_mid: int,
    source_type: SourceTypeEnum,
    source_id: str,
    biz_id: str | None = None,
) -> str:
    """计算事件幂等键。

    未传 `biz_id` 时，同一个人对同一实体的同类行为只会记一条
    （反复点赞取消点赞不会刷屏）；传了 `biz_id`（如评论 id）则按业务实体区分，
    同一个人的多条回复各记一条。
    """
    raw = f"{mid}:{event_type}:{actor_mid}:{source_type}:{source_id}:{biz_id or ''}"
    return hashlib.md5(raw.encode("utf-8")).hexdigest()


class EventService:
    """事件提醒的上报、聚合查询与已读管理。"""

    # ==================== 上报 ====================

    @staticmethod
    async def report(session: AsyncSession, req: EventReportReq) -> EventReportResp:
        """上报一条用户行为事件。

        事件提醒是站内信的一种：落库即送达，由接收方通过 /list / msg_feed 轮询读取，
        不再做任何第三方渠道推送或「实时 / 批量」分流。
        """
        # 闸门一：用户是否愿意接收这类提醒
        accepted = await SettingService.accept_event(session, req.mid, req.event_type)
        if not accepted:
            logger.debug(f"用户 {req.mid} 已关闭 {req.event_type} 提醒，跳过")
            return EventReportResp(accepted=False, duplicated=False)

        # 闸门二：不给自己发提醒
        if req.mid == req.actor_mid:
            return EventReportResp(accepted=False, duplicated=False)

        dedup_key = build_dedup_key(
            req.mid,
            req.event_type,
            req.actor_mid,
            req.source_type,
            req.source_id,
            req.biz_id,
        )

        row = EventMessage(
            mid=req.mid,
            event_type=req.event_type,
            source_type=req.source_type,
            source_id=req.source_id,
            source_title=req.source_title,
            source_cover=req.source_cover,
            actor_mid=req.actor_mid,
            actor_name=req.actor_name,
            actor_avatar=req.actor_avatar,
            content=req.content,
            jump_url=req.jump_url,
            dedup_key=dedup_key,
        )
        session.add(row)
        try:
            await session.commit()
            await session.refresh(row)
        except IntegrityError:
            # 命中 dedup_key 唯一索引：重复上报，直接返回既有事件
            await session.rollback()
            existing = (
                await session.exec(
                    select(EventMessage).where(EventMessage.dedup_key == dedup_key)
                )
            ).one_or_none()
            return EventReportResp(
                accepted=True,
                event_id=existing.id if existing else None,
                duplicated=True,
            )

        return EventReportResp(
            accepted=True,
            event_id=row.id,
            duplicated=False,
        )

    # ==================== 聚合查询 ====================

    @staticmethod
    async def aggregate(
        session: AsyncSession,
        mid: int,
        event_type: EventTypeEnum | None = None,
        page_num: int = 1,
        page_size: int = 20,
        only_unread: bool = False,
    ) -> tuple[list[EventAggregateItem], int]:
        """按 source_type + source_id 聚合展示。

        SQL 层只做分组统计（COUNT / SUM / MAX），拿到本页的分组键之后
        再回捞一次明细补齐「最新内容」与「头像列表」，
        避免在 GROUP BY 里做复杂的窗口函数，MySQL 8 以下也能跑。
        """
        conditions = [EventMessage.mid == mid, EventMessage.is_deleted == False]  # noqa: E712
        if event_type is not None:
            conditions.append(EventMessage.event_type == event_type)
        if only_unread:
            conditions.append(EventMessage.is_read == False)  # noqa: E712

        group_cols = (
            EventMessage.event_type,
            EventMessage.source_type,
            EventMessage.source_id,
        )

        # 分组总数
        subq = (
            select(*group_cols).where(*conditions).group_by(*group_cols).subquery()
        )
        total = int(
            (await session.exec(select(func.count()).select_from(subq))).one() or 0
        )

        unread_expr = func.sum(case((EventMessage.is_read == False, 1), else_=0))  # noqa: E712
        stmt = (
            select(
                *group_cols,
                func.count().label("cnt"),
                unread_expr.label("unread"),
                func.max(EventMessage.id).label("latest_id"),
            )
            .where(*conditions)
            .group_by(*group_cols)
            .order_by(func.max(EventMessage.id).desc())
            .offset((page_num - 1) * page_size)
            .limit(page_size)
        )
        groups = (await session.exec(stmt)).all()
        if not groups:
            return [], total

        # 回捞明细：一次查完本页所有分组的事件，在内存里取每组最新的若干条
        group_keys = [(g[0], g[1], g[2]) for g in groups]
        detail_stmt = (
            select(EventMessage)
            .where(
                *conditions,
                tuple_(*group_cols).in_(group_keys),  # type: ignore[arg-type]
            )
            .order_by(EventMessage.id.desc())  # type: ignore[union-attr]
            .limit(_ACTOR_SCAN_LIMIT)
        )
        details = list((await session.exec(detail_stmt)).all())

        bucket: dict[tuple, list[EventMessage]] = {}
        for row in details:
            bucket.setdefault(
                (row.event_type, row.source_type, row.source_id), []
            ).append(row)

        items: list[EventAggregateItem] = []
        for etype, stype, sid, cnt, unread, latest_id in groups:
            rows = bucket.get((etype, stype, sid), [])
            latest = rows[0] if rows else None
            # 头像去重：同一个人多条事件只展示一次
            actors: list[EventActorBrief] = []
            seen: set[int] = set()
            for r in rows:
                if r.actor_mid in seen:
                    continue
                seen.add(r.actor_mid)
                actors.append(
                    EventActorBrief(
                        actor_mid=r.actor_mid,
                        actor_name=r.actor_name,
                        actor_avatar=r.actor_avatar,
                    )
                )
                if len(actors) >= _MAX_ACTORS_PER_GROUP:
                    break
            items.append(
                EventAggregateItem(
                    event_type=etype,
                    source_type=stype,
                    source_id=sid,
                    source_title=latest.source_title if latest else None,
                    source_cover=latest.source_cover if latest else None,
                    jump_url=latest.jump_url if latest else None,
                    count=int(cnt or 0),
                    unread_count=int(unread or 0),
                    actors=actors,
                    latest_event_id=int(latest_id or 0),
                    latest_content=latest.content if latest else None,
                    latest_at=latest.created_at if latest else None,
                )
            )
        return items, total

    @staticmethod
    async def list_detail(
        session: AsyncSession,
        mid: int,
        event_type: EventTypeEnum | None = None,
        source_type: SourceTypeEnum | None = None,
        source_id: str | None = None,
        page_num: int = 1,
        page_size: int = 20,
        only_unread: bool = False,
    ) -> tuple[list[EventItem], int]:
        """查看某个聚合分组下的事件明细（点开卡片后的列表）。"""
        conditions = [EventMessage.mid == mid, EventMessage.is_deleted == False]  # noqa: E712
        if event_type is not None:
            conditions.append(EventMessage.event_type == event_type)
        if source_type is not None:
            conditions.append(EventMessage.source_type == source_type)
        if source_id is not None:
            conditions.append(EventMessage.source_id == source_id)
        if only_unread:
            conditions.append(EventMessage.is_read == False)  # noqa: E712

        total = int(
            (
                await session.exec(
                    select(func.count()).select_from(EventMessage).where(*conditions)
                )
            ).one()
            or 0
        )
        stmt = (
            select(EventMessage)
            .where(*conditions)
            .order_by(EventMessage.id.desc())  # type: ignore[union-attr]
            .offset((page_num - 1) * page_size)
            .limit(page_size)
        )
        rows = (await session.exec(stmt)).all()
        items = [
            EventItem(
                id=r.id or 0,
                event_type=r.event_type,
                source_type=r.source_type,
                source_id=r.source_id,
                source_title=r.source_title,
                source_cover=r.source_cover,
                actor_mid=r.actor_mid,
                actor_name=r.actor_name,
                actor_avatar=r.actor_avatar,
                content=r.content,
                jump_url=r.jump_url,
                is_read=r.is_read,
                created_at=r.created_at,
            )
            for r in rows
        ]
        return items, total

    # ==================== 已读管理 ====================

    @staticmethod
    async def mark_read(
        session: AsyncSession, mid: int, req: EventReadReq
    ) -> EventReadResp:
        """标记已读，支持 id / 类型 / 聚合分组三种粒度。"""
        table = EventMessage.__table__
        conditions = [table.c.mid == mid, table.c.is_read == False]  # noqa: E712

        if req.event_ids:
            conditions.append(table.c.id.in_(req.event_ids))
        else:
            if req.event_type is not None:
                conditions.append(table.c.event_type == req.event_type)
            if req.source_type is not None:
                conditions.append(table.c.source_type == req.source_type)
            if req.source_id is not None:
                conditions.append(table.c.source_id == req.source_id)

        now = datetime.now()
        result = await session.exec(
            table.update().where(*conditions).values(  # type: ignore[call-overload]
                is_read=True, read_at=now, updated_at=now
            )
        )
        affected = int(getattr(result, "rowcount", 0) or 0)

        # 同步推进类型级已读游标，便于「全部已读」后快速判断红点
        if req.event_type is not None and not req.event_ids:
            await EventService._advance_cursor(session, mid, req.event_type)

        await session.commit()
        return EventReadResp(
            affected=affected,
            unread_count=await EventService.count_unread(session, mid, req.event_type),
        )

    @staticmethod
    async def delete(session: AsyncSession, mid: int, event_ids: list[int]) -> int:
        if not event_ids:
            return 0
        table = EventMessage.__table__
        result = await session.exec(
            table.update()  # type: ignore[call-overload]
            .where(table.c.mid == mid, table.c.id.in_(event_ids))
            .values(is_deleted=True, updated_at=datetime.now())
        )
        await session.commit()
        return int(getattr(result, "rowcount", 0) or 0)

    @staticmethod
    async def count_unread(
        session: AsyncSession, mid: int, event_type: EventTypeEnum | None = None
    ) -> int:
        conditions = [
            EventMessage.mid == mid,
            EventMessage.is_read == False,  # noqa: E712
            EventMessage.is_deleted == False,  # noqa: E712
        ]
        if event_type is not None:
            conditions.append(EventMessage.event_type == event_type)
        stmt = select(func.count()).select_from(EventMessage).where(*conditions)
        return int((await session.exec(stmt)).one() or 0)

    @staticmethod
    async def count_unread_by_type(session: AsyncSession, mid: int) -> dict[str, int]:
        """一次查询拿到各类型未读数（前端红点）。"""
        stmt = (
            select(EventMessage.event_type, func.count())
            .where(
                EventMessage.mid == mid,
                EventMessage.is_read == False,  # noqa: E712
                EventMessage.is_deleted == False,  # noqa: E712
            )
            .group_by(EventMessage.event_type)
        )
        return {str(etype): int(cnt) for etype, cnt in (await session.exec(stmt)).all()}

    @staticmethod
    async def _advance_cursor(
        session: AsyncSession, mid: int, event_type: EventTypeEnum
    ) -> None:
        max_id = (
            await session.exec(
                select(func.max(EventMessage.id)).where(
                    EventMessage.mid == mid, EventMessage.event_type == event_type
                )
            )
        ).one() or 0
        stmt = select(EventReadCursor).where(
            EventReadCursor.mid == mid, EventReadCursor.event_type == event_type
        )
        row = (await session.exec(stmt)).one_or_none()
        if row is None:
            row = EventReadCursor(mid=mid, event_type=event_type)
        row.last_read_id = max(row.last_read_id, int(max_id))
        row.last_read_at = datetime.now()
        session.add(row)


__all__ = ["EventService", "build_dedup_key"]
