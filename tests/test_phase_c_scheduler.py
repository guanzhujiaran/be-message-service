"""Phase C 定时任务端到端验证。

对真实 MySQL 验证关键语义（站内信送达由 DB 写路径保证，不再经第三方推送）：
- C1 系统通知投递标记：`dispatch_notify_job` 取出未投递通知 → 置位 dispatched（去重）
- C3 死信补偿：`retry_dead_letter_job` 补写失败私信正文 → 标记 resolved + content_ready

MQ 投递统一打桩（monkeypatch publisher / DmContentService.write），只验证任务编排与去重逻辑。
"""

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlmodel.ext.asyncio.session import AsyncSession as SQLModelAsyncSession
from sqlmodel import select

from app.core import database as db_mod
from app.core.config import settings
from app.core.database import new_session
from app.models.db import (
    DmContentDeadLetter,
    EventMessage,
    NotifyMessage,
    UserActivity,
    UserMessageSetting,
)
from app.models.enums import DmMsgTypeEnum
from app.models.schemas import NotifyCreateReq
from app.services.dm import DmContentService
from app.services.notify import NotifyService
from app.services.setting import SettingService
import importlib

# app.tasks.__init__ 把 `scheduler` 重导出成了 AsyncIOScheduler 实例，
# 直接 `from app.tasks.scheduler import ...` 会命中实例而非模块，故显式按模块路径加载。
sched_mod = importlib.import_module("app.tasks.scheduler")


@pytest.fixture(autouse=True)
async def _bind_engine_per_test():
    engine = create_async_engine(
        settings.mysql_message_url,
        pool_pre_ping=True,
        future=True,
        connect_args={"charset": "utf8mb4", "autocommit": False},
    )
    db_mod.engine = engine
    db_mod.async_session_maker = async_sessionmaker(
        bind=engine,
        class_=SQLModelAsyncSession,
        expire_on_commit=False,
        autoflush=False,
    )
    yield
    await engine.dispose()


M = {
    "c_notify_target": 910001,
    "c_notify_creator": 910002,
    "c_batch": 910010,
    "c_dm_dlq": 910020,
}
ALL_MIDS = set(M.values())


async def _purge_notify() -> None:
    async with new_session() as s:
        for tbl in ("msg_notify_state", "msg_notify_cursor", "msg_notify"):
            await s.exec(text(f"DELETE FROM {tbl}"))
        await s.commit()


async def _cleanup() -> None:
    async with new_session() as s:
        for stmt in [
            select(NotifyMessage).where(NotifyMessage.creator_mid.in_(ALL_MIDS)),
            select(UserMessageSetting).where(UserMessageSetting.mid.in_(ALL_MIDS)),
            select(EventMessage).where(EventMessage.mid.in_(ALL_MIDS)),
            select(UserActivity).where(UserActivity.mid.in_(ALL_MIDS)),
            select(DmContentDeadLetter).where(DmContentDeadLetter.sender_uid.in_(ALL_MIDS)),
            select(DmContentDeadLetter).where(DmContentDeadLetter.receiver_uid.in_(ALL_MIDS)),
        ]:
            for r in (await s.exec(stmt)).all():
                await s.delete(r)
        await s.commit()


async def test_c1_dispatch_notify_job() -> None:
    """C1：已到发布时间的通知被标记 dispatched，重复跑不重复处理。

    注意：系统通知是读扩散，发布后即对用户可见（站内信），
    投递本身不依赖任何第三方推送渠道；此任务仅维护「已投递」状态与去重。
    """
    await _purge_notify()
    await _cleanup()

    async with new_session() as s:
        await SettingService.get(s, M["c_notify_target"])
        notify = await NotifyService.create(
            s,
            M["c_notify_creator"],
            NotifyCreateReq(title="t", content="c", publish_now=True),
        )
        assert notify.dispatched is False

    await sched_mod.dispatch_notify_job()

    # 已到发布时间的通知被标记为 dispatched
    async with new_session() as s:
        n = await s.get(NotifyMessage, notify.id)
        assert n is not None and n.dispatched is True

    # 再次跑任务不应重复处理（fetch_dispatchable 不会再捞到）
    await sched_mod.dispatch_notify_job()
    async with new_session() as s:
        left = await NotifyService.fetch_dispatchable(s)
        assert left == [], "已投递通知不应再出现在待投递列表"
    await _cleanup()


async def test_c3_retry_dead_letter_job(monkeypatch) -> None:
    """C3：死信正文被补写成功并标记 resolved。"""
    await _cleanup()
    written: list = []

    async def fake_write(payload):
        written.append(payload)
        return True

    monkeypatch.setattr(DmContentService, "write", fake_write)

    async with new_session() as s:
        s.add(
            DmContentDeadLetter(
                msgkey=999000001,
                session_key="sk",
                sender_uid=M["c_dm_dlq"],
                receiver_uid=910021,
                msg_type=DmMsgTypeEnum.TEXT,
                content="hello",
                msg_ts=1700000000,
                resolved=False,
                retry_count=0,
            )
        )
        await s.commit()

    await sched_mod.retry_dead_letter_job()
    assert len(written) == 1, "DmContentService.write 应被调用 1 次"

    async with new_session() as s:
        dl = (
            await s.exec(
                select(DmContentDeadLetter).where(DmContentDeadLetter.msgkey == 999000001)
            )
        ).one()
        assert dl.resolved is True, "死信应标记已解决"
    await _cleanup()
