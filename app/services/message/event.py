"""事件提醒服务（兼容层 / 薄壳）。

2.50.0 起重构为面向对象模型，见 :mod:`app.services.message.events`：

- 每种 ``EventTypeEnum`` 对应一个事件处理器类（``LikeEvent`` / ``ReplyEvent`` / ...），
  继承自虚基类 ``BaseEvent``，差异逻辑通过抽象方法 ``build_msgfeed_content`` 实现；
- 上报：``BaseEvent.from_req(req).report(session)``；
- 读路径：``BaseEvent.aggregate / list_msgfeed / mark_read / ...``（跨类型集合操作保留为类方法）。

本模块仅作为**兼容薄壳**，把所有 ``EventService.*`` 调用委托给新对象模型，
新代码请直接使用对象（``events.BaseEvent`` / 各具体事件类），不要再依赖 ``EventService``。
"""

from app.services.message.events import (
    AtEvent,
    AuditRejectEvent,
    BaseEvent,
    EVENT_REGISTRY,
    GenericEvent,
    HideEvent,
    LikeEvent,
    MsgfeedBuildContext,
    ReplyEvent,
    ReportRejectEvent,
    ReportResolvedEvent,
    build_dedup_key,
)

__all__ = [
    "EventService",
    "build_dedup_key",
    "BaseEvent",
    "GenericEvent",
    "LikeEvent",
    "ReplyEvent",
    "AtEvent",
    "AuditRejectEvent",
    "HideEvent",
    "ReportRejectEvent",
    "ReportResolvedEvent",
    "MsgfeedBuildContext",
    "EVENT_REGISTRY",
]


class EventService:
    """兼容薄壳：委托给 ``events.BaseEvent`` 对象模型。

    新代码请直接用 ``BaseEvent`` / 具体事件类，例如::

        await BaseEvent.from_req(req).report(session)
        items, total = await BaseEvent.aggregate(session, mid, EventTypeEnum.LIKE)
    """

    # ==================== 上报 ====================

    @staticmethod
    async def report(session, req):
        return await BaseEvent.from_req(req).report(session)

    # ==================== 聚合查询 ====================

    @staticmethod
    async def aggregate(session, mid, event_type=None, page_num=1, page_size=20, only_unread=False):
        return await BaseEvent.aggregate(
            session, mid, event_type, page_num, page_size, only_unread
        )

    @staticmethod
    async def list_detail(
        session, mid, event_type=None, source_type=None, source_id=None,
        page_num=1, page_size=20, only_unread=False,
    ):
        return await BaseEvent.list_detail(
            session, mid, event_type, source_type, source_id,
            page_num, page_size, only_unread,
        )

    @staticmethod
    async def list_msgfeed(
        session, mid, event_type=None, cursor_id=None, page_size=20, only_unread=False,
    ):
        return await BaseEvent.list_msgfeed(
            session, mid, event_type, cursor_id, page_size, only_unread
        )

    # ==================== 已读管理 ====================

    @staticmethod
    async def mark_read(session, mid, req):
        return await BaseEvent.mark_read(session, mid, req)

    @staticmethod
    async def delete(session, mid, event_ids):
        return await BaseEvent.delete(session, mid, event_ids)

    @staticmethod
    async def count_unread(session, mid, event_type=None):
        return await BaseEvent.count_unread(session, mid, event_type)

    @staticmethod
    async def count_unread_by_type(session, mid):
        return await BaseEvent.count_unread_by_type(session, mid)

    @staticmethod
    async def _advance_cursor(session, mid, event_type):
        return await BaseEvent._advance_cursor(session, mid, event_type)
