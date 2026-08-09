"""Phase B 接口/服务层集成自测。

直接驱动各模块的 Service 核心方法，对真实 MySQL 验证关键语义：
- 系统通知：游标去重、已读、删除、按受众(CUSTOM)可见性
- 事件提醒：聚合分组、dedup_key 幂等、按类型未读、已读/删除
- 私信：写扩散、陌生人过滤、msgkey 游标翻页、撤回时间窗、已读 ack、删除
- 消息设置：默认开关、事件闸门、免打扰时段
- 活跃度：实时判定
- msg_feed：跨模块未读汇总

说明：**站内信（系统通知 / 事件提醒 / 私信）的送达由数据库写路径保证，
不再经由第三方推送渠道**，因此相关用例不再打桩 `publish_dm_notify` 等已删除的
推送投递函数。RabbitMQ 与私信分片库不在本机可用，仅把 `publisher.publish_dm_content`
/ `DmContentService` 的外部依赖打桩，只验证主库索引/会话/设置等核心逻辑。
"""

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlmodel.ext.asyncio.session import AsyncSession as SQLModelAsyncSession

from app.core import database as db_mod
from app.core.config import settings
from app.core.database import new_session
from app.models.db import (
    DmContentDeadLetter,
    DmMessageIndex,
    DmSession,
    EventMessage,
    EventReadCursor,
    NotifyCursor,
    NotifyMessage,
    NotifyState,
    UserActivity,
    UserMessageSetting,
)
from app.models.enums import (
    DmMsgStatusEnum,
    DmMsgTypeEnum,
    DmRelationEnum,
    EventTypeEnum,
    NotifyTargetTypeEnum,
    SourceTypeEnum,
)
from app.models.schemas import (
    DmSendReq,
    EventReadReq,
    EventReportReq,
    EventUnreadResp,
    MessageSettingUpdateReq,
    NotifyCreateReq,
)
from app.services import publisher
from app.services import dm as dm_svc_mod
from app.services.activity import ActivityService
from app.services.dm import DmService
from app.services.event import EventService, build_dedup_key
from app.services.notify import NotifyService
from app.services.setting import SettingService
from bili_common.models.depends import AuthInfo
from sqlmodel import select, text

# 每个测试函数跑在独立的事件循环里；模块级 engine 会绑死在第一个循环上，
# 导致后续测试报 "Event loop is closed"。这里用 autouse 异步 fixture 在每个测试
# 自己的循环里重建 engine / sessionmaker，并绑定到 app.core.database，规避该问题。
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


# 统一的测试 mid 区间，便于清理
M = {
    "notify_user": 900001,
    "notify_custom": 900002,
    "event_user": 900003,
    "dm_sender": 900010,
    "dm_receiver": 900011,
    "dm_stranger_sender": 900020,
    "dm_stranger_receiver": 900021,
    "setting_user": 900030,
    "activity_active": 900040,
    "activity_batch": 900041,
    "feed_user": 900050,
    "notify_lv0": 900060,
    "notify_lv6": 900061,
    "notify_vip": 900062,
}
ALL_MIDS = set(M.values())


async def _purge_notify() -> None:
    """清空系统通知三张表，保证 notify 相关测试在干净数据上断言精确计数。

    共享开发库里常残留历史 notify 行（ALL 等），会把 `pull` 的首页填满，
    导致「只应有 1 条」之类的断言失真；故 notify 测试开始前整体清空这三张表。
    """
    async with new_session() as s:
        for tbl in ("msg_notify_state", "msg_notify_cursor", "msg_notify"):
            await s.exec(text(f"DELETE FROM {tbl}"))
        await s.commit()


async def _cleanup() -> None:
    """删除本次测试产生的所有行，避免污染。"""
    async with new_session() as s:
        for stmt in [
            select(NotifyMessage).where(NotifyMessage.creator_mid.in_(ALL_MIDS)),
            select(NotifyCursor).where(NotifyCursor.mid.in_(ALL_MIDS)),
            select(NotifyState).where(NotifyState.mid.in_(ALL_MIDS)),
            select(EventMessage).where(EventMessage.mid.in_(ALL_MIDS)),
            select(EventReadCursor).where(EventReadCursor.mid.in_(ALL_MIDS)),
            select(DmMessageIndex).where(DmMessageIndex.owner_mid.in_(ALL_MIDS)),
            select(DmSession).where(DmSession.owner_mid.in_(ALL_MIDS)),
            select(UserMessageSetting).where(UserMessageSetting.mid.in_(ALL_MIDS)),
            select(UserActivity).where(UserActivity.mid.in_(ALL_MIDS)),
            select(DmContentDeadLetter).where(
                DmContentDeadLetter.sender_uid.in_(ALL_MIDS)
            ),
        ]:
            rows = (await s.exec(stmt)).all()
            for r in rows:
                await s.delete(r)
        # DmMessageIndex 也按 talker_mid / sender_uid 清理
        for stmt in [
            select(DmMessageIndex).where(DmMessageIndex.talker_mid.in_(ALL_MIDS)),
            select(DmMessageIndex).where(DmMessageIndex.sender_uid.in_(ALL_MIDS)),
            select(DmSession).where(DmSession.talker_mid.in_(ALL_MIDS)),
            select(DmContentDeadLetter).where(
                DmContentDeadLetter.receiver_uid.in_(ALL_MIDS)
            ),
        ]:
            rows = (await s.exec(stmt)).all()
            for r in rows:
                await s.delete(r)
        await s.commit()


async def test_notify_cursor_dedup_and_visibility() -> None:
    await _purge_notify()
    user = AuthInfo(mid=M["notify_user"], role="normal", level=0)
    async with new_session() as s:
        # 发布一条面向全站的已发布通知（用测试 mid 作为创建者，便于清理）
        created = await NotifyService.create(
            s, M["notify_user"], NotifyCreateReq(title="t", content="c", publish_now=True)
        )
        # 第一次拉取：应返回该通知并推进游标
        r1 = await NotifyService.pull(s, user)
        assert len(r1.items) == 1, "首次 pull 应返回新通知"
        assert r1.items[0].id == created.id
        assert r1.cursor == created.id, "游标应推进到通知 id"
        assert r1.unread_count == 1

        # 第二次拉取（不传 cursor）：游标已推进，不应重复返回 → 去重生效
        r2 = await NotifyService.pull(s, user)
        assert r2.items == [], "游标去重：二次 pull 不应重复返回"

        # 全部已读后未读数归零
        await NotifyService.mark_read(s, user, None)
        assert await NotifyService.unread_count(s, user) == 0

        # 删除后：仅看未读列表为空
        await NotifyService.delete_for_user(s, user, [created.id])
        items, total = await NotifyService.list_for_user(s, user, only_unread=True)
        assert total == 0, "删除后仅看未读应为空"

    # 受众可见性：CUSTOM 只投放给指定 mid
    await _purge_notify()
    async with new_session() as s:
        custom = await NotifyService.create(
            s,
            M["notify_user"],
            NotifyCreateReq(
                title="t2",
                content="c2",
                target_type=NotifyTargetTypeEnum.CUSTOM,
                target_value=str(M["notify_custom"]),
                publish_now=True,
            ),
        )
        other = AuthInfo(mid=M["notify_user"], role="normal", level=0)
        target = AuthInfo(mid=M["notify_custom"], role="normal", level=0)
        ro = await NotifyService.pull(s, other)
        rt = await NotifyService.pull(s, target)
        assert all(i.id != custom.id for i in ro.items), "非目标用户不应看到 CUSTOM 通知"
        assert any(i.id == custom.id for i in rt.items), "目标用户应看到 CUSTOM 通知"
    await _purge_notify()


async def test_notify_level_role_vip_visibility() -> None:
    """验证按等级(LEVEL)/角色(ROLE)/大会员(VIP)投放的可见性判定。"""
    await _purge_notify()
    admin = M["notify_user"]
    async with new_session() as s:
        lv5 = await NotifyService.create(
            s,
            admin,
            NotifyCreateReq(
                title="lv5",
                content="c",
                target_type=NotifyTargetTypeEnum.LEVEL,
                target_value="5",
                publish_now=True,
            ),
        )
        role_normal = await NotifyService.create(
            s,
            admin,
            NotifyCreateReq(
                title="role-normal",
                content="c",
                target_type=NotifyTargetTypeEnum.ROLE,
                target_value="normal",
                publish_now=True,
            ),
        )
        vip = await NotifyService.create(
            s,
            admin,
            NotifyCreateReq(
                title="vip",
                content="c",
                target_type=NotifyTargetTypeEnum.VIP,
                publish_now=True,
            ),
        )
        alln = await NotifyService.create(
            s, admin, NotifyCreateReq(title="all", content="c", publish_now=True)
        )

        async def pull_as(mid: int, **kw) -> set[int]:
            user = AuthInfo(mid=mid, **kw)
            # 用独立会话读取，模拟真实 HTTP 请求（创建与拉取分属不同连接）
            async with new_session() as ps:
                cur = await NotifyService._get_or_create_cursor(ps, mid)
                cur.last_notify_id = 0
                ps.add(cur)
                await ps.commit()
                resp = await NotifyService.pull(ps, user)
                return {i.id for i in resp.items}

        # level=0 普通用户：不满足 LEVEL(5>0)、不满足 VIP → 应见 role + all
        ids_a = await pull_as(M["notify_lv0"], role="normal", level=0)
        assert lv5.id not in ids_a, "level=0 不应命中 LEVEL(5)"
        assert vip.id not in ids_a, "非 VIP 不应命中 VIP"
        assert role_normal.id in ids_a and alln.id in ids_a

        # level=6 普通用户：满足 LEVEL(5<=6)，不满足 VIP → 见 lv5 + role + all
        ids_b = await pull_as(M["notify_lv6"], role="normal", level=6)
        assert lv5.id in ids_b and role_normal.id in ids_b and alln.id in ids_b
        assert vip.id not in ids_b, "非 VIP 不应命中 VIP"

        # level=0 大会员：满足 VIP → 见 role + vip + all，不满足 LEVEL(5>0)
        ids_c = await pull_as(M["notify_vip"], role="normal", level=0, vip_status="1")
        assert vip.id in ids_c and role_normal.id in ids_c and alln.id in ids_c
        assert lv5.id not in ids_c, "level=0 不应命中 LEVEL(5)"
    await _cleanup()


async def test_event_aggregation_and_dedup() -> None:
    await _cleanup()
    mid = M["event_user"]
    async with new_session() as s:
        req = lambda actor: EventReportReq(  # noqa: E731
            mid=mid,
            event_type=EventTypeEnum.LIKE,
            source_type=SourceTypeEnum.VIDEO,
            source_id="BV1",
            actor_mid=actor,
            actor_name=f"user{actor}",
        )
        # 同一人对同一来源重复上报 → 去重
        r1 = await EventService.report(s, req(800001))
        r2 = await EventService.report(s, req(800001))
        assert r1.accepted and not r1.duplicated
        assert r2.duplicated, "同人同来源重复上报应去重"

        # 不同人对同一来源 → 两条明细，聚合为 count=2
        await EventService.report(s, req(800002))

        groups, total = await EventService.aggregate(s, mid, EventTypeEnum.LIKE)
        assert total == 1, "应聚合成 1 个分组"
        assert groups[0].count == 2, "聚合 count 应为 2"
        assert groups[0].unread_count == 2

        by_type = await EventService.count_unread_by_type(s, mid)
        assert by_type.get("like") == 2, "like 未读应为 2"

        # 按类型一键已读
        await EventService.mark_read(
            s, mid, EventReadReq(event_type=EventTypeEnum.LIKE)  # type: ignore[arg-type]
        )
        assert await EventService.count_unread(s, mid) == 0

        # 删除
        rows = (await s.exec(select(EventMessage).where(EventMessage.mid == mid))).all()
        await EventService.delete(s, mid, [r.id for r in rows])
        assert await EventService.count_unread(s, mid) == 0
    await _cleanup()


async def test_dm_write_diffusion_recall_and_stranger(
    monkeypatch,
) -> None:
    await _cleanup()

    async def _ok(*a, **k):
        return True

    async def _noop_none(*a, **k):
        return None

    async def _noop_dict(*a, **k):
        return {}

    # 打桩外部依赖：MQ 投递与分片读写
    monkeypatch.setattr(publisher, "publish_dm_content", _ok)
    monkeypatch.setattr(dm_svc_mod.DmContentService, "batch_get", _noop_dict)
    monkeypatch.setattr(dm_svc_mod.DmContentService, "clear_content", _noop_none)
    monkeypatch.setattr(dm_svc_mod.DmContentService, "write", _ok)

    sender, receiver = M["dm_sender"], M["dm_receiver"]
    async with new_session() as s:
        resp = await DmService.send(
            s,
            sender,
            "senderName",
            DmSendReq(receiver_mid=receiver, content="hello", msg_type=DmMsgTypeEnum.TEXT),
        )
        assert not resp.filtered, "默认应正常送达"
        mk = int(resp.msgkey)

        # 写扩散：收发双方各有会话与索引行
        recv_sessions = (
            await s.exec(select(DmSession).where(DmSession.owner_mid == receiver))
        ).all()
        send_sessions = (
            await s.exec(select(DmSession).where(DmSession.owner_mid == sender))
        ).all()
        assert len(recv_sessions) == 1 and len(send_sessions) == 1
        assert recv_sessions[0].unread_count == 1, "接收方未读应为 1"
        assert send_sessions[0].unread_count == 0

        # 聊天记录：msgkey 游标翻页，内容缺失回落摘要
        msgs = await DmService.list_messages(s, receiver, sender)
        assert len(msgs.items) == 1
        assert msgs.items[0].content == "hello", "内容缺失应回落为摘要"

        # 已读 ack 清零未读
        await DmService.ack(s, receiver, sender)
        assert await DmService.count_unread(s, receiver) == 0

        # 撤回：发送者在时间窗内可撤回，双方状态变 RECALLED
        ok, msg = await DmService.recall_message(s, sender, mk)
        assert ok, f"撤回应成功: {msg}"
        after = (
            await s.exec(
                select(DmMessageIndex).where(DmMessageIndex.msgkey == mk)
            )
        ).all()
        assert all(r.msg_status is DmMsgStatusEnum.RECALLED for r in after)

        # 非发送者不可撤回
        ok2, _ = await DmService.recall_message(s, receiver, mk)
        assert not ok2, "非发送者不应能撤回"

        # 删除：仅自己视角不可见
        await DmService.delete_messages(s, sender, [mk])
        my_msgs = await DmService.list_messages(s, sender, receiver)
        assert my_msgs.items == [], "删除后自己视角应看不到"

    # 陌生人过滤：接收方关闭陌生人私信 → 仅写发送方视角
    await _cleanup()
    async with new_session() as s:
        await SettingService.update(
            s, M["dm_stranger_receiver"], MessageSettingUpdateReq(recv_stranger_dm=False)
        )
        resp2 = await DmService.send(
            s,
            M["dm_stranger_sender"],
            "stranger",
            DmSendReq(
                receiver_mid=M["dm_stranger_receiver"],
                content="hi",
                msg_type=DmMsgTypeEnum.TEXT,
            ),
        )
        assert resp2.filtered, "关闭陌生人私信应被过滤"
        recv_sessions = (
            await s.exec(
                select(DmSession).where(DmSession.owner_mid == M["dm_stranger_receiver"])
            )
        ).all()
        assert recv_sessions == [], "被过滤时接收方不应有会话"
    await _cleanup()


async def test_setting_gate_and_dnd() -> None:
    await _cleanup()
    mid = M["setting_user"]
    async with new_session() as s:
        setting = await SettingService.get(s, mid)
        assert setting.recv_like and setting.push_enabled, "默认应全开"

        # 关闭点赞提醒 → 事件闸门关闭
        await SettingService.update(s, mid, MessageSettingUpdateReq(recv_like=False))
        assert await SettingService.accept_event(s, mid, EventTypeEnum.LIKE) is False
        assert await SettingService.accept_event(s, mid, EventTypeEnum.REPLY) is True

        # 免打扰时段 [0,23) → 当前小时必然在区间内 → 不允许推送
        await SettingService.update(
            s, mid, MessageSettingUpdateReq(dnd_start_hour=0, dnd_end_hour=23)
        )
        assert await SettingService.can_push_now(s, mid) is False

        # 非阻塞时段（0:00~0:00 为空区间）→ 始终允许推送
        await SettingService.update(
            s, mid, MessageSettingUpdateReq(dnd_start_hour=0, dnd_end_hour=0)
        )
        assert await SettingService.can_push_now(s, mid) is True
    await _cleanup()


async def test_activity_active_judgement() -> None:
    """活跃度仅用于判定用户是否在线（驱动前端轮询节奏），与站内信送达无关。"""
    await _cleanup()
    async with new_session() as s:
        # 刚 touch 的用户视为活跃
        await ActivityService.touch(s, M["activity_active"])
        assert await ActivityService.is_active(s, M["activity_active"]) is True
        snap = await ActivityService.get_snapshot(s, M["activity_active"])
        assert snap.is_active is True

        # 从未活跃过的用户视为非活跃
        assert await ActivityService.is_active(s, M["activity_batch"]) is False
        snap2 = await ActivityService.get_snapshot(s, M["activity_batch"])
        assert snap2.is_active is False
    await _cleanup()


async def test_msg_feed_unread_aggregation() -> None:
    await _cleanup()
    await _purge_notify()
    mid = M["feed_user"]
    user = AuthInfo(mid=mid, role="normal", level=0)
    async with new_session() as s:
        # 造一条通知 + 一条 like 事件 + 一发私信（用桩）
        await NotifyService.create(
            s, M["feed_user"], NotifyCreateReq(title="tf", content="cf", publish_now=True)
        )
        await EventService.report(
            s,
            EventReportReq(
                mid=mid,
                event_type=EventTypeEnum.LIKE,
                source_type=SourceTypeEnum.VIDEO,
                source_id="BVfeed",
                actor_mid=700001,
            ),
        )

        notify_unread = await NotifyService.unread_count(s, user)
        event_by_type = await EventService.count_unread_by_type(s, mid)
        dm_unread = await DmService.count_unread(s, mid)

        resp = EventUnreadResp(
            like=event_by_type.get("like", 0),
            reply=event_by_type.get("reply", 0),
            at=event_by_type.get("at", 0),
            notify=notify_unread,
            dm=dm_unread,
            total=notify_unread + sum(event_by_type.values()) + dm_unread,
        )
        assert resp.notify == 1, "未读通知应为 1"
        assert resp.like == 1, "未读 like 应为 1"
        assert resp.total >= 2
    await _cleanup()
