"""私信（单聊）服务：写扩散 + 内容异步化。

## 发送链路

```
POST /dm/send
   ├─ 1. 陌生人过滤（查接收方消息设置）
   ├─ 2. 生成 msgkey（雪花ID，内嵌毫秒时间戳）
   ├─ 3. 同步写主库：索引 ×2（收发双方视角）+ 会话 ×2       ← 决定「消息立刻可见」
   ├─ 4. 异步投递正文到 MQ  → 消费者路由到月度库分表写入      ← 抬高写性能天花板
   └─ 5. 异步投递到达提醒（按活跃度决定实时/批量）
```

**为什么内容要异步**：正文可能很长且要跨库路由（可能触发建库建表 DDL），
把它留在同步链路里会直接决定发送接口的 RT。剥离之后，同步部分只剩
两条定长索引行 + 两条会话行的写入，写性能天花板由此抬高。

**异步不影响读的可用性**：索引行冗余了 `content_preview`（摘要）与
`content_ready` 标记。正文分片还没落库时，读接口直接返回摘要，
用户看到的仍是完整的会话流；分片落库后 `content_ready` 置位，
后续读取自动切到完整正文。

**失败不丢消息**：MQ 投递失败时按配置降级为同步写入；同步也失败则落
`msg_dm_content_dlq` 死信表，由定时任务重试补偿，保证正文最终一致。
"""

from datetime import datetime

from loguru import logger
from sqlmodel import col, func, or_, select, update
from sqlmodel.ext.asyncio.session import AsyncSession

from app.core.config import settings
from app.core.sharding import generate_msgkey, parse_timestamp_ms
from app.models.db import DmContentDeadLetter, DmMessageIndex, DmSession
from app.models.enums import (
    DmAuditStateEnum,
    DmMsgStatusEnum,
    DmMsgTypeEnum,
    DmRelationEnum,
)
from app.models.schemas import (
    DmContentPayload,
    DmMessageItem,
    DmMessageListResp,
    DmSendReq,
    DmSendResp,
    DmSessionItem,
    DmSessionListResp,
)
from app.services import publisher
from app.services.activity import ActivityService
from app.services.dm_content import DmContentService
from app.services.follow import FollowService
from app.services.notify import NotifyService
from app.services.setting import SettingService

# 会话列表展示的正文摘要长度
_PREVIEW_LEN = 100


def make_session_key(a: int, b: int) -> str:
    """会话键：小 mid_大 mid，保证双方算出的键一致。"""
    lo, hi = (a, b) if a <= b else (b, a)
    return f"{lo}_{hi}"


def _preview(content: str, msg_type: DmMsgTypeEnum) -> str:
    if msg_type is DmMsgTypeEnum.IMAGE:
        return "[图片]"
    text = content.replace("\n", " ").strip()
    return text[:_PREVIEW_LEN]


class DmService:
    """私信会话与消息的读写。"""

    # ==================== 发送 ====================

    @staticmethod
    async def send(
        session: AsyncSession,
        sender_mid: int,
        sender_name: str | None,
        req: DmSendReq,
    ) -> DmSendResp:
        """发送一条私信（写扩散）。"""
        receiver_mid = req.receiver_mid
        if sender_mid == receiver_mid:
            raise ValueError("不能给自己发送私信")

        session_key = make_session_key(sender_mid, receiver_mid)
        msgkey = generate_msgkey()
        msg_ts = parse_timestamp_ms(msgkey)
        preview = _preview(req.content, req.msg_type)

        # ---- 0. 先审后发开关 ----
        # 开启后新私信先进入审核态，对接收方不可见，需管理端通过后(置 normal)才可见；
        # 发送者本人视角始终可见（与评论先审后发对齐）。
        is_auditing = settings.dm_pre_audit
        audit_state = (
            DmAuditStateEnum.AUDITING if is_auditing else DmAuditStateEnum.NORMAL
        )

        # ---- 拦截：接收方已拉黑发送方 → 直接拒绝发送（比陌生人过滤优先级更高）----
        if await FollowService.is_blocked_by(session, sender_mid, receiver_mid):
            raise ValueError("对方已拉黑你，无法发送私信")

        # ---- 1. 陌生人过滤 ----
        is_stranger = await DmService._is_stranger(session, receiver_mid, sender_mid)
        filtered = False
        if is_stranger and not await SettingService.accept_stranger_dm(
            session, receiver_mid
        ):
            # 对方关闭了陌生人私信：消息只写发送方视角，接收方完全无感知
            filtered = True
            logger.debug(
                f"用户 {receiver_mid} 关闭陌生人私信，来自 {sender_mid} 的消息被过滤"
            )

        # ---- 2. 写扩散：索引行 ----
        owners: list[int] = [sender_mid] if filtered else [sender_mid, receiver_mid]
        for owner in owners:
            session.add(
                DmMessageIndex(
                    owner_mid=owner,
                    talker_mid=receiver_mid if owner == sender_mid else sender_mid,
                    session_key=session_key,
                    msgkey=msgkey,
                    sender_uid=sender_mid,
                    msg_type=req.msg_type,
                    msg_status=DmMsgStatusEnum.NORMAL,
                    msg_ts=msg_ts,
                    content_preview=preview,
                    content_ready=False,
                    audit_state=audit_state,
                )
            )

        # ---- 3. 写扩散：会话行 ----
        await DmService._upsert_session(
            session,
            owner_mid=sender_mid,
            talker_mid=receiver_mid,
            session_key=session_key,
            msgkey=msgkey,
            preview=preview,
            msg_ts=msg_ts,
            sender_uid=sender_mid,
            incr_unread=False,
            talker_name=req.receiver_name,
            talker_avatar=req.receiver_avatar,
            # 主动发起方视角永远是普通会话
            relation=DmRelationEnum.NORMAL,
        )
        if not filtered:
            await DmService._upsert_session(
                session,
                owner_mid=receiver_mid,
                talker_mid=sender_mid,
                session_key=session_key,
                msgkey=msgkey,
                preview="[私信审核中]" if is_auditing else preview,
                msg_ts=msg_ts,
                sender_uid=sender_mid,
                # 审核中：先不发未读红点，待管理端通过后再在 set_state 里补
                incr_unread=not is_auditing,
                talker_name=sender_name,
                talker_avatar=None,
                relation=(
                    DmRelationEnum.STRANGER if is_stranger else DmRelationEnum.NORMAL
                ),
            )
        await session.commit()

        # 进入审核态：弱依赖地通知发送者（不影响发送主流程）
        if is_auditing:
            try:
                await NotifyService.send_to_user(
                    sender_mid,
                    title="私信审核中",
                    content="您发送的私信正在审核中，通过后将对方可见。",
                )
            except Exception as e:  # noqa: BLE001
                logger.warning(f"私信审核中通知投递失败（弱依赖，已忽略）: {e}")

        # 发送者本人算一次活跃
        await ActivityService.touch(session, sender_mid)

        # ---- 4. 正文异步落库 ----
        payload = DmContentPayload(
            msgkey=msgkey,
            session_key=session_key,
            sender_uid=sender_mid,
            receiver_uid=receiver_mid,
            msg_type=req.msg_type,
            content=req.content,
            msg_ts=msg_ts,
        )
        content_async = await publisher.publish_dm_content(payload)
        if not content_async:
            await DmService._fallback_write_content(session, payload)

        # ---- 5. 站内信送达 ----
        # 接收方的未读计数与会话快照已在上面的写扩散（步骤 3）中完成，
        # 私信属于站内信，不向第三方推送渠道发消息；前端通过轮询
        # msg_feed / heartbeat 感知新私信红点。

        return DmSendResp(
            msgkey=str(msgkey),
            session_key=session_key,
            msg_ts=msg_ts,
            filtered=filtered,
            content_async=content_async,
        )

    @staticmethod
    async def _fallback_write_content(
        session: AsyncSession, payload: DmContentPayload
    ) -> None:
        """MQ 不可用时的降级：同步写分片，再失败则进死信表等待补偿。"""
        if settings.dm_content_sync_fallback and await DmContentService.write(payload):
            await DmService.mark_content_ready(session, payload.msgkey)
            logger.warning(f"MQ 不可用，msgkey={payload.msgkey} 已同步写入分片")
            return
        session.add(
            DmContentDeadLetter(
                msgkey=payload.msgkey,
                session_key=payload.session_key,
                sender_uid=payload.sender_uid,
                receiver_uid=payload.receiver_uid,
                msg_type=payload.msg_type,
                content=payload.content,
                msg_ts=payload.msg_ts,
                last_error="MQ 投递失败且同步写入未成功",
            )
        )
        await session.commit()
        logger.error(f"msgkey={payload.msgkey} 正文写入失败，已进入死信表等待补偿")

    @staticmethod
    async def mark_content_ready(session: AsyncSession, msgkey: int) -> None:
        """正文落库完成后，把双方索引行的 content_ready 置位。"""
        await session.exec(  # type: ignore[call-overload]
            update(DmMessageIndex)
            .where(col(DmMessageIndex.msgkey) == msgkey)
            .values(content_ready=True, updated_at=datetime.now())
        )
        await session.commit()

    # ==================== 会话列表 ====================

    @staticmethod
    async def list_sessions(
        session: AsyncSession,
        owner_mid: int,
        relation: DmRelationEnum | None = None,
        page_num: int = 1,
        page_size: int | None = None,
    ) -> DmSessionListResp:
        """会话列表。

        `relation` 用于把「陌生人消息」折叠成独立分组：
        传 NORMAL 得到主列表，传 STRANGER 得到陌生人列表，不传则全部。
        """
        page_size = page_size or settings.dm_default_page_size
        conditions = [DmSession.owner_mid == owner_mid, DmSession.is_deleted == False]  # noqa: E712
        if relation is not None:
            conditions.append(DmSession.relation == relation)

        total = int(
            (
                await session.exec(
                    select(func.count()).select_from(DmSession).where(*conditions)
                )
            ).one()
            or 0
        )
        stmt = (
            select(DmSession)
            .where(*conditions)
            .order_by(col(DmSession.is_top).desc(), col(DmSession.last_msg_ts).desc())  # type: ignore[union-attr]
            .offset((page_num - 1) * page_size)
            .limit(page_size)
        )
        rows = (await session.exec(stmt)).all()

        # 未读汇总：主列表红点与陌生人红点分开展示
        unread_stmt = (
            select(DmSession.relation, func.sum(DmSession.unread_count))
            .where(DmSession.owner_mid == owner_mid, DmSession.is_deleted == False)  # noqa: E712
            .group_by(DmSession.relation)
        )
        unread_map = {
            str(rel): int(cnt or 0)
            for rel, cnt in (await session.exec(unread_stmt)).all()
        }

        return DmSessionListResp(
            items=[DmService._to_session_item(r) for r in rows],
            total=total,
            unread_total=sum(unread_map.values()),
            stranger_unread=unread_map.get(str(DmRelationEnum.STRANGER), 0),
        )

    @staticmethod
    async def delete_session(
        session: AsyncSession, owner_mid: int, talker_mid: int
    ) -> int:
        """删除会话（仅自己不可见）。"""
        row = await DmService._get_session(session, owner_mid, talker_mid)
        if row is None:
            return 0
        row.is_deleted = True
        row.unread_count = 0
        row.updated_at = datetime.now()
        session.add(row)
        await session.commit()
        return 1

    # ==================== 聊天记录 ====================

    @staticmethod
    async def list_messages(
        session: AsyncSession,
        owner_mid: int,
        talker_mid: int,
        cursor: int | None = None,
        page_size: int | None = None,
    ) -> DmMessageListResp:
        """拉取聊天记录（按 msgkey 游标倒序翻页）。

        读取分两步：先从主库拿索引（轻量、走联合索引），
        再按 msgkey 批量回捞分片里的正文。正文缺失时用摘要兜底。
        """
        page_size = page_size or settings.dm_default_page_size
        conditions = [
            DmMessageIndex.owner_mid == owner_mid,
            DmMessageIndex.talker_mid == talker_mid,
            DmMessageIndex.msg_status != DmMsgStatusEnum.DELETED,
            # 先审后发：审核中的私信对「非发送者」不可见，发送者本人始终可见
            or_(
                DmMessageIndex.audit_state != DmAuditStateEnum.AUDITING,
                DmMessageIndex.sender_uid == owner_mid,
            ),
        ]
        if cursor:
            conditions.append(DmMessageIndex.msgkey < cursor)

        stmt = (
            select(DmMessageIndex)
            .where(*conditions)
            .order_by(col(DmMessageIndex.msgkey).desc())  # type: ignore[union-attr]
            .limit(page_size + 1)
        )
        rows = list((await session.exec(stmt)).all())
        has_more = len(rows) > page_size
        rows = rows[:page_size]

        # 只有正常态消息才需要回捞正文（撤回的不展示内容）
        need_content = [
            r.msgkey for r in rows if r.msg_status is DmMsgStatusEnum.NORMAL
        ]
        contents = await DmContentService.batch_get(need_content)

        items: list[DmMessageItem] = []
        for r in rows:
            if r.msg_status is DmMsgStatusEnum.RECALLED:
                content, ready = None, True
            elif r.audit_state in (DmAuditStateEnum.REJECTED, DmAuditStateEnum.HIDDEN):
                # 被管理端驳回 / 下架：保留气泡位置，内容替换为占位文案
                content = (
                    "[该消息已被管理员下架]"
                    if r.audit_state is DmAuditStateEnum.HIDDEN
                    else "[该消息已被管理员驳回]"
                )
                ready = True
            else:
                hit = contents.get(r.msgkey)
                # 分片未命中（异步落库尚未完成）→ 回落摘要，保证可读
                content = hit if hit is not None else r.content_preview
                ready = hit is not None
            items.append(
                DmMessageItem(
                    msgkey=str(r.msgkey),
                    sender_uid=r.sender_uid,
                    msg_type=r.msg_type,
                    msg_status=r.msg_status,
                    content=content,
                    msg_ts=r.msg_ts,
                    content_ready=ready,
                    created_at=r.created_at,
                    audit_state=r.audit_state,
                )
            )

        # 进入会话即视为一次活跃行为
        await ActivityService.touch(session, owner_mid)

        return DmMessageListResp(
            items=items,
            cursor=str(rows[-1].msgkey) if rows else None,
            has_more=has_more,
            talker_mid=talker_mid,
            session_key=make_session_key(owner_mid, talker_mid),
        )

    # ==================== 删除与撤回 ====================

    @staticmethod
    async def delete_messages(
        session: AsyncSession, owner_mid: int, msgkeys: list[int]
    ) -> int:
        """删除消息：只标记自己视角的索引行，对方仍能看到。"""
        if not msgkeys:
            return 0
        result = await session.exec(  # type: ignore[call-overload]
            update(DmMessageIndex)
            .where(
                col(DmMessageIndex.owner_mid) == owner_mid,
                col(DmMessageIndex.msgkey).in_(msgkeys),
            )
            .values(msg_status=DmMsgStatusEnum.DELETED, updated_at=datetime.now())
        )
        await session.commit()
        return int(getattr(result, "rowcount", 0) or 0)

    @staticmethod
    async def recall_message(
        session: AsyncSession, operator_mid: int, msgkey: int
    ) -> tuple[bool, str]:
        """撤回消息：双方均不可见，且物理抹掉分片里的正文。

        限制：只有发送者本人可撤回，且必须在 `dm_recall_window_seconds` 内。
        时间判定直接用 msgkey 内嵌的时间戳，无需回查数据库。
        """
        row = (
            await session.exec(
                select(DmMessageIndex).where(
                    DmMessageIndex.owner_mid == operator_mid,
                    DmMessageIndex.msgkey == msgkey,
                )
            )
        ).one_or_none()
        if row is None:
            return False, "消息不存在"
        if row.sender_uid != operator_mid:
            return False, "只能撤回自己发送的消息"
        if row.msg_status is DmMsgStatusEnum.RECALLED:
            return True, "消息已撤回"

        elapsed = (
            datetime.now().timestamp() * 1000 - parse_timestamp_ms(msgkey)
        ) / 1000
        if elapsed > settings.dm_recall_window_seconds:
            return False, f"超过 {settings.dm_recall_window_seconds} 秒的消息不可撤回"

        now = datetime.now()
        # 撤回是双向的：一次更新掉收发双方的索引行
        await session.exec(  # type: ignore[call-overload]
            update(DmMessageIndex)
            .where(col(DmMessageIndex.msgkey) == msgkey)
            .values(
                msg_status=DmMsgStatusEnum.RECALLED,
                content_preview="[消息已撤回]",
                recalled_at=now,
                updated_at=now,
            )
        )
        # 会话列表的最后一条快照同步刷新
        await session.exec(  # type: ignore[call-overload]
            update(DmSession)
            .where(col(DmSession.last_msgkey) == msgkey)
            .values(last_content_preview="[消息已撤回]", updated_at=now)
        )
        await session.commit()

        await DmContentService.clear_content(msgkey)
        return True, "撤回成功"

    # ==================== 已读 ====================

    @staticmethod
    async def ack(
        session: AsyncSession,
        owner_mid: int,
        talker_mid: int,
        ack_msgkey: int | None = None,
    ) -> int:
        """标记会话已读：未读清零并抬高已读水位。"""
        row = await DmService._get_session(session, owner_mid, talker_mid)
        if row is None:
            return 0
        row.unread_count = 0
        if ack_msgkey is not None:
            row.ack_msgkey = max(row.ack_msgkey or 0, ack_msgkey)
        else:
            row.ack_msgkey = row.last_msgkey
        row.updated_at = datetime.now()
        session.add(row)
        await session.commit()
        return 1

    @staticmethod
    async def count_unread(session: AsyncSession, owner_mid: int) -> int:
        stmt = select(func.sum(DmSession.unread_count)).where(
            DmSession.owner_mid == owner_mid,
            DmSession.is_deleted == False,  # noqa: E712
        )
        return int((await session.exec(stmt)).one() or 0)

    # ==================== 内部方法 ====================

    @staticmethod
    async def _is_stranger(
        session: AsyncSession, receiver_mid: int, sender_mid: int
    ) -> bool:
        """判断发送者对接收者而言是否为陌生人。

        判定规则（任一成立即为熟人）：
        1. 接收方已有与对方的会话且被标记为普通关系；
        2. 接收方曾经给对方发过消息（说明主动建立过联系）。
        """
        existing = await DmService._get_session(session, receiver_mid, sender_mid)
        if existing is not None and existing.relation is DmRelationEnum.NORMAL:
            return False
        replied = (
            await session.exec(
                select(func.count())
                .select_from(DmMessageIndex)
                .where(
                    DmMessageIndex.owner_mid == receiver_mid,
                    DmMessageIndex.talker_mid == sender_mid,
                    DmMessageIndex.sender_uid == receiver_mid,
                )
            )
        ).one() or 0
        return int(replied) == 0

    @staticmethod
    async def _get_session(
        session: AsyncSession, owner_mid: int, talker_mid: int
    ) -> DmSession | None:
        stmt = select(DmSession).where(
            DmSession.owner_mid == owner_mid, DmSession.talker_mid == talker_mid
        )
        return (await session.exec(stmt)).one_or_none()

    @staticmethod
    async def _upsert_session(
        session: AsyncSession,
        owner_mid: int,
        talker_mid: int,
        session_key: str,
        msgkey: int,
        preview: str,
        msg_ts: int,
        sender_uid: int,
        incr_unread: bool,
        talker_name: str | None,
        talker_avatar: str | None,
        relation: DmRelationEnum,
    ) -> None:
        """更新（或创建）某一方视角的会话行。"""
        row = await DmService._get_session(session, owner_mid, talker_mid)
        if row is None:
            row = DmSession(
                owner_mid=owner_mid,
                talker_mid=talker_mid,
                session_key=session_key,
                relation=relation,
            )
        elif (
            row.relation is DmRelationEnum.STRANGER
            and relation is DmRelationEnum.NORMAL
        ):
            # 陌生人回复后升级为普通会话，从陌生人分组移出
            row.relation = DmRelationEnum.NORMAL

        if talker_name:
            row.talker_name = talker_name
        if talker_avatar:
            row.talker_avatar = talker_avatar

        row.last_msgkey = msgkey
        row.last_content_preview = preview
        row.last_msg_ts = msg_ts
        row.last_sender_uid = sender_uid
        row.is_deleted = False
        if incr_unread:
            row.unread_count += 1
        row.updated_at = datetime.now()
        session.add(row)

    @staticmethod
    def _to_session_item(row: DmSession) -> DmSessionItem:
        return DmSessionItem(
            talker_mid=row.talker_mid,
            talker_name=row.talker_name,
            talker_avatar=row.talker_avatar,
            session_key=row.session_key,
            last_msgkey=str(row.last_msgkey) if row.last_msgkey else None,
            last_content_preview=row.last_content_preview,
            last_msg_ts=row.last_msg_ts,
            last_sender_uid=row.last_sender_uid,
            unread_count=row.unread_count,
            relation=row.relation,
            is_top=row.is_top,
            is_muted=row.is_muted,
            updated_at=row.updated_at,
        )

    # ==================== 死信补偿 ====================

    @staticmethod
    async def retry_dead_letters(session: AsyncSession, limit: int = 50) -> int:
        """重试正文写入失败的死信，保证内容最终一致。"""
        stmt = (
            select(DmContentDeadLetter)
            .where(DmContentDeadLetter.resolved == False)  # noqa: E712
            .order_by(col(DmContentDeadLetter.id).asc())  # type: ignore[union-attr]
            .limit(limit)
        )
        rows = list((await session.exec(stmt)).all())
        succeeded = 0
        for row in rows:
            ok = await DmContentService.write(
                DmContentPayload(
                    msgkey=row.msgkey,
                    session_key=row.session_key,
                    sender_uid=row.sender_uid,
                    receiver_uid=row.receiver_uid,
                    msg_type=row.msg_type,
                    content=row.content or "",
                    msg_ts=row.msg_ts,
                )
            )
            row.retry_count += 1
            if ok:
                row.resolved = True
                succeeded += 1
                await DmService.mark_content_ready(session, row.msgkey)
            else:
                row.last_error = "重试写入仍失败"
            session.add(row)
        if rows:
            await session.commit()
        if succeeded:
            logger.info(f"死信补偿成功 {succeeded}/{len(rows)} 条私信正文")
        return succeeded


__all__ = ["DmService", "make_session_key"]
