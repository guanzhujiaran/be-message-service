"""消息设置服务。

用户可自定义是否接收点赞 / 回复 / @提醒以及陌生人私信。
这份设置是整个消息系统的**第一道闸门**：事件上报、私信投递、通知推送
在落库或推送前都会先查询这里，被关闭的类型直接短路，不产生后续流量。

无 Redis 的前提下，设置数据量极小（每用户一行）且读多写极少，
MySQL 主键 / 唯一索引点查完全够用。
"""

from datetime import datetime

from loguru import logger
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.db import UserMessageSetting
from app.models.enums import EventTypeEnum
from app.models.schemas import MessageSettingResp, MessageSettingUpdateReq

# 默认设置：用户从未配置过时的兜底值（全部开启）
_DEFAULTS: dict[str, bool] = {
    "recv_like": True,
    "recv_reply": True,
    "recv_at": True,
    "recv_stranger_dm": True,
    "recv_notify": True,
    "push_enabled": True,
}

# 事件类型 → 设置字段的映射
_EVENT_FIELD_MAP: dict[EventTypeEnum, str] = {
    EventTypeEnum.LIKE: "recv_like",
    EventTypeEnum.REPLY: "recv_reply",
    EventTypeEnum.AT: "recv_at",
}


class SettingService:
    """消息设置读写。"""

    @staticmethod
    async def get_or_create(session: AsyncSession, mid: int) -> UserMessageSetting:
        """获取用户设置，不存在则按默认值创建。"""
        stmt = select(UserMessageSetting).where(UserMessageSetting.mid == mid)
        row = (await session.exec(stmt)).one_or_none()
        if row is not None:
            return row

        row = UserMessageSetting(mid=mid, **_DEFAULTS)
        session.add(row)
        try:
            await session.commit()
            await session.refresh(row)
        except Exception:
            # 并发下可能被别的请求抢先插入，回滚后重新读取即可
            await session.rollback()
            row = (await session.exec(stmt)).one_or_none()
            if row is None:
                raise
        return row

    @staticmethod
    async def get(session: AsyncSession, mid: int) -> MessageSettingResp:
        row = await SettingService.get_or_create(session, mid)
        return MessageSettingResp(
            mid=row.mid,
            recv_like=row.recv_like,
            recv_reply=row.recv_reply,
            recv_at=row.recv_at,
            recv_stranger_dm=row.recv_stranger_dm,
            recv_notify=row.recv_notify,
            push_enabled=row.push_enabled,
            dnd_start_hour=row.dnd_start_hour,
            dnd_end_hour=row.dnd_end_hour,
            updated_at=row.updated_at,
        )

    @staticmethod
    async def update(
        session: AsyncSession, mid: int, req: MessageSettingUpdateReq
    ) -> MessageSettingResp:
        """部分更新：只写入本次显式传入的字段。"""
        row = await SettingService.get_or_create(session, mid)
        changes = req.model_dump(exclude_unset=True, exclude_none=True)
        for field, value in changes.items():
            setattr(row, field, value)
        row.updated_at = datetime.now()
        session.add(row)
        await session.commit()
        await session.refresh(row)
        logger.debug(f"用户 {mid} 更新消息设置: {changes}")
        return await SettingService.get(session, mid)

    # ==================== 闸门判定 ====================

    @staticmethod
    async def accept_event(
        session: AsyncSession, mid: int, event_type: EventTypeEnum
    ) -> bool:
        """判断用户是否接收该类型的事件提醒。"""
        row = await SettingService.get_or_create(session, mid)
        field = _EVENT_FIELD_MAP.get(event_type)
        if field is None:
            return True
        return bool(getattr(row, field, True))

    @staticmethod
    async def accept_stranger_dm(session: AsyncSession, mid: int) -> bool:
        """判断用户是否接收陌生人私信。"""
        row = await SettingService.get_or_create(session, mid)
        return row.recv_stranger_dm

    @staticmethod
    async def accept_notify(session: AsyncSession, mid: int) -> bool:
        """判断用户是否接收系统通知。"""
        row = await SettingService.get_or_create(session, mid)
        return row.recv_notify

    @staticmethod
    async def can_push_now(session: AsyncSession, mid: int) -> bool:
        """判断当前时刻是否允许推送到外部渠道（含免打扰时段判定）。"""
        row = await SettingService.get_or_create(session, mid)
        if not row.push_enabled:
            return False
        start, end = row.dnd_start_hour, row.dnd_end_hour
        if start is None or end is None:
            return True
        hour = datetime.now().hour
        if start <= end:
            in_dnd = start <= hour < end
        else:
            # 跨零点的免打扰区间，如 22:00 ~ 08:00
            in_dnd = hour >= start or hour < end
        return not in_dnd

    @staticmethod
    async def bulk_ensure(session: AsyncSession, mids: list[int]) -> None:
        """批量确保一批用户存在设置行（定时任务批量推送前调用）。

        使用 MySQL 的 `INSERT ... ON DUPLICATE KEY UPDATE` 做幂等批量插入，
        避免逐个 SELECT 再 INSERT 的 N+1 往返。
        """
        if not mids:
            return
        stmt = mysql_insert(UserMessageSetting.__table__).values(
            [
                {"mid": mid, "created_at": datetime.now(), "updated_at": datetime.now(), **_DEFAULTS}
                for mid in set(mids)
            ]
        )
        # 命中唯一键时不做任何实质修改，仅保持行存在
        stmt = stmt.on_duplicate_key_update(mid=stmt.inserted.mid)
        await session.exec(stmt)  # type: ignore[call-overload]
        await session.commit()


__all__ = ["SettingService"]
