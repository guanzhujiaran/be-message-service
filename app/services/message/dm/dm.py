"""私信（单聊）领域层：写扩散 + 内容异步化。

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

## 对象模型

本模块不再提供扁平的 `DmService` 静态方法，而是把领域对象显式建模：

- `DmSessionObject`：一段会话（owner_mid 与 talker_mid 之间）。所有围绕
  这段对话的行为（发送 / 拉取 / 已读 / 撤回 / 删消息 / 删会话）都是它的
  **实例方法**，构造时即绑定 `session / owner_mid / talker_mid`。
- `DmInbox`：某用户的私信收件箱（会话集合 + 未读汇总），承载 owner 级的
  批量操作（列表 / 未读总数）。
- `mark_content_ready` / `retry_dead_letters`：与具体某段会话无关的
  系统级维护动作，保留为模块级函数。
"""

from datetime import datetime
from typing import Any

from loguru import logger
from sqlalchemy import case
from sqlalchemy.exc import OperationalError
from sqlmodel import col, func, or_, select, update
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlmodel.ext.asyncio.session import AsyncSession

from bili_common.models import ResponseCode
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
import app.services.message.infrastructure.publisher as publisher
from app.services.message.insite.activity import ActivityService
from app.services.message.dm.dm_content import DmContentService
from app.services.user.follow import FollowService
from app.models.schemas import CommentUserBrief
from app.services.user.account import PptrUser
from app.services.message.insite.notify import NotifyService
from app.services.message.insite.setting import SettingService

# 会话列表展示的正文摘要长度
_PREVIEW_LEN = 100


def make_session_key(a: int | str, b: int | str) -> str:
    """会话键：小 mid_大 mid，保证双方算出的键一致。"""
    a, b = int(a), int(b)
    lo, hi = (a, b) if a <= b else (b, a)
    return f"{lo}_{hi}"


def _preview(content: str, msg_type: DmMsgTypeEnum) -> str:
    if msg_type is DmMsgTypeEnum.IMAGE:
        return "[图片]"
    text = content.replace("\n", " ").strip()
    return text[:_PREVIEW_LEN]


# 私信写扩散落库的死锁重试上限：批量并发私信时，不同会话行的并发插入会触发
# MySQL gap lock / 插入意图锁交互导致死锁（1213，事务被回滚）。死锁是瞬时的，
# 按同一 msgkey 回滚重试即可成功（不会产生重复消息），超过上限才抛错。
_DM_DEADLOCK_RETRIES = 3


def _is_deadlock(e: Exception) -> bool:
    """MySQL 死锁（错误码 1213）：并发写扩散被回滚，事务可安全重试。"""
    orig = getattr(e, "orig", None)
    if orig is None:
        return False
    args = getattr(orig, "args", None)
    return bool(args and args[0] == 1213)


# ==================== 发送限制异常 ====================
# 两类「发送被限」是带独立业务码的主动拒绝（消息不落库），须在 API 层先于通用
# ValueError（→400）/ Exception（→500）捕获并按各自 code 回执，故不与 ValueError 混用。


class DmDailySendLimitError(Exception):
    """触发每日发送上限（dm_daily_send_limit）。"""

    code = ResponseCode.DM_SEND_DAILY_LIMIT

    def __init__(self, limit: int) -> None:
        super().__init__(f"今日私信发送已达上限（{limit} 条），请明天再试")


class DmStrangerSendLimitError(Exception):
    """触发陌生人单条闸门（对方未关注且未回过消息时仅可发一条）。"""

    code = ResponseCode.DM_SEND_STRANGER_LIMIT

    def __init__(self, limit: int) -> None:
        super().__init__(
            f"对方尚未关注你且未回复过你，只能发送 {limit} 条消息，待对方回复后可继续发送"
        )


# ==================== 共享底层查询 ====================


async def _get_session_row(
    session: AsyncSession, owner_mid: int, talker_mid: int
) -> DmSession | None:
    """加载某 owner 视角下与 talker 的会话行（不存在返回 None）。"""
    stmt = select(DmSession).where(
        DmSession.owner_mid == owner_mid, DmSession.talker_mid == talker_mid
    )
    return (await session.exec(stmt)).one_or_none()


def _to_session_item(
    row: DmSession, user_cache: dict[int, CommentUserBrief]
) -> DmSessionItem:
    """把会话 ORM 行拼成对外展示的 `DmSessionItem`。

    `user_cache` 是 `talker_mid -> CommentUserBrief`，由调用方批量拉取后传入，
    优先用实时用户卡片，缺漏时回落到会话快照里存储的 name/avatar。
    """
    uc = user_cache.get(row.talker_mid)
    return DmSessionItem(
        talker_mid=row.talker_mid,
        talker_name=(uc.uname if uc else None) or row.talker_name,
        talker_avatar=(uc.avatar if uc else None) or row.talker_avatar,
        session_key=row.session_key,
        last_msgkey=str(row.last_msgkey) if row.last_msgkey else None,
        last_content_preview=row.last_content_preview,
        last_msg_ts=row.last_msg_ts,
        last_sender_uid=row.last_sender_uid,
        unread_count=row.unread_count,
        relation=row.relation,
        is_top=(row.top_ts != 0),
        top_ts=row.top_ts,
        is_muted=row.is_muted,
        updated_at=row.updated_at,
    )


# ==================== 系统级维护函数 ====================


async def mark_content_ready(session: AsyncSession, msgkey: int) -> None:
    """正文落库完成后，把双方索引行的 content_ready 置位。"""
    await session.exec(  # type: ignore[call-overload]
        update(DmMessageIndex)
        .where(col(DmMessageIndex.msgkey) == msgkey)
        .values(content_ready=True, updated_at=datetime.now())
    )
    await session.commit()


async def retry_dead_letters(session: AsyncSession, limit: int = 50) -> int:
    """重试正文写入失败的死信，保证内容最终一致。"""
    stmt = (
        select(DmContentDeadLetter)
        .where(DmContentDeadLetter.resolved == False)
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
            await mark_content_ready(session, row.msgkey)
        else:
            row.last_error = "重试写入仍失败"
        session.add(row)
    if rows:
        await session.commit()
    if succeeded:
        logger.info(f"死信补偿成功 {succeeded}/{len(rows)} 条私信正文")
    return succeeded


# ==================== 单个会话对象 ====================


class DmSessionObject:
    """单个私信会话（owner_mid 与 talker_mid 之间）的领域对象。

    该对象即「一段对话」：所有围绕这段对话的行为都作为实例方法挂载，
    构造时即绑定 `session / owner_mid / talker_mid`，调用处不再到处传这三个参数。
    需要 owner 级批量操作的场景走 `DmInbox`。
    """

    def __init__(
        self,
        session: AsyncSession,
        owner_mid: int,
        talker_mid: int | None = None,
        *,
        session_key: str | None = None,
    ) -> None:
        self.session = session
        self.owner_mid = owner_mid
        self.talker_mid = talker_mid
        # session_key 对称（小 mid_大 mid），仅在需要时才惰性计算，
        # 这样不需要 talker 的操作（如按 msgkey 撤回 / 删除消息）也能构造本对象。
        self._session_key = session_key

    @property
    def session_key(self) -> str:
        if self._session_key is None:
            self._session_key = make_session_key(self.owner_mid, self.talker_mid)
        return self._session_key

    @classmethod
    def between(
        cls, session: AsyncSession, owner_mid: int, talker_mid: int
    ) -> "DmSessionObject":
        """构造一段会话对象（语义化工厂方法）。"""
        return cls(session, owner_mid, talker_mid)

    # ==================== 加载 ====================

    async def load(self) -> DmSession | None:
        """加载本 owner 视角的会话行（不存在返回 None）。"""
        return await _get_session_row(self.session, self.owner_mid, self.talker_mid)

    async def to_item(self) -> DmSessionItem | None:
        """拼成对外展示用的 `DmSessionItem`（顺带回捞对端用户卡片）。"""
        row = await self.load()
        if row is None:
            return None
        user_cache = await PptrUser.get_many([row.talker_mid])
        return _to_session_item(row, user_cache)

    # ==================== 发送 ====================

    async def send(
        self, req: DmSendReq, sender_name: str | None = None
    ) -> DmSendResp:
        """以 self.owner_mid 身份向 self.talker_mid 发送一条私信（写扩散）。"""
        sender_mid = self.owner_mid
        receiver_mid = self.talker_mid
        if sender_mid == receiver_mid:
            raise ValueError("不能给自己发送私信")

        # ---- 账号状态校验：收发双方都必须存在且状态正常（未注销 / 未软删）----
        # 发送方即便是持有合法 JWT 的已注销账号，也必须在此被拦下，避免产生业务数据。
        if not await PptrUser.exists_active(sender_mid):
            raise ValueError("账号不存在或已停用，无法发送私信")
        if not await PptrUser.exists_active(receiver_mid):
            raise ValueError("对方账号不存在或已停用，无法发送私信")

        session_key = self.session_key
        msgkey = await generate_msgkey()
        msg_ts = parse_timestamp_ms(msgkey)
        preview = _preview(req.content, req.msg_type)

        # ---- 0. 先审后发开关 ----
        is_auditing = settings.dm_pre_audit
        audit_state = (
            DmAuditStateEnum.AUDITING if is_auditing else DmAuditStateEnum.NORMAL
        )

        # ---- 拦截：接收方已拉黑发送方 → 直接拒绝发送 ----
        if await FollowService.is_blocked_by(self.session, sender_mid, receiver_mid):
            raise ValueError("对方已拉黑你，无法发送私信")

        # ---- 发送限制（2.57.0，命中一律不落库）----
        await self._check_daily_send_limit(sender_mid)
        await self._check_stranger_gate(sender_mid, receiver_mid)

        # ---- 1. 陌生人过滤 ----
        is_stranger = await self._is_stranger(receiver_mid, sender_mid)
        filtered = False
        if is_stranger and not await SettingService.accept_stranger_dm(
            self.session, receiver_mid
        ):
            filtered = True
            logger.debug(
                f"用户 {receiver_mid} 关闭陌生人私信，来自 {sender_mid} 的消息被过滤"
            )

        # ---- 2 + 3. 写扩散落库（索引行 + 会话行）----
        # 批量并发私信时，不同会话行的并发插入会触发 MySQL 死锁（1213，
        # gap lock / 插入意图锁交互，事务被 MySQL 回滚）。死锁是瞬时的，
        # 捕获后按**同一 msgkey** 回滚重试即可成功，不会产生重复消息。
        for attempt in range(_DM_DEADLOCK_RETRIES + 1):
            try:
                await self._persist_dm(
                    req=req,
                    sender_name=sender_name,
                    session_key=session_key,
                    msgkey=msgkey,
                    msg_ts=msg_ts,
                    preview=preview,
                    is_auditing=is_auditing,
                    audit_state=audit_state,
                    is_stranger=is_stranger,
                    filtered=filtered,
                )
                break
            except OperationalError as e:
                if not _is_deadlock(e) or attempt >= _DM_DEADLOCK_RETRIES:
                    raise
                await self.session.rollback()
                logger.warning(
                    f"私信发送写扩散死锁（{sender_mid}→{receiver_mid}），"
                    f"回滚重试 {attempt + 1}/{_DM_DEADLOCK_RETRIES}"
                )

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
        await ActivityService.touch(self.session, sender_mid)

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
            await self._fallback_write_content(payload)

        # ---- 5. 站内信送达 ----
        # 接收方的未读计数与会话快照已在上面的写扩散中完成，私信属于站内信，
        # 不向第三方推送渠道发消息；前端通过轮询 msg_feed / heartbeat 感知新私信红点。

        return DmSendResp(
            msgkey=str(msgkey),
            session_key=session_key,
            msg_ts=msg_ts,
            filtered=filtered,
            content_async=content_async,
        )

    async def _persist_dm(
        self,
        *,
        req: DmSendReq,
        sender_name: str | None,
        session_key: str,
        msgkey: int,
        msg_ts: int,
        preview: str,
        is_auditing: bool,
        audit_state: DmAuditStateEnum,
        is_stranger: bool,
        filtered: bool,
    ) -> None:
        """索引行 + 会话行写扩散落库（一个事务）。

        独立成方法以支持死锁（MySQL 1213）回滚重试：`send` 捕获死锁后
        ``session.rollback()`` 再按**同一 msgkey** 重调本方法，
        重试不会产生重复消息。
        """
        sender_mid = self.owner_mid
        receiver_mid = self.talker_mid

        # ---- 2. 写扩散：索引行 ----
        owners: list[int] = [sender_mid] if filtered else [sender_mid, receiver_mid]
        for owner in owners:
            self.session.add(
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

        # ---- 3. 写扩散：会话行（主动发起方视角永远是普通会话）----
        # 统一按「owner_mid 小者先行」的顺序 upsert 收发双方视角行：
        # 并发双向发送（A→B 与 B→A）若都以发送方视角先行，会以相反顺序
        # 获取 (A,B) / (B,A) 两行的锁，形成写扩散死锁（MySQL 1213）。
        # 按固定顺序取锁可消除该模式的死锁；各视角行的参数语义保持不变。
        session_rows: list[tuple[int, Any, dict]] = [
            (
                sender_mid,
                self._upsert_owner_row,
                {
                    "msgkey": msgkey,
                    "preview": preview,
                    "msg_ts": msg_ts,
                    "sender_uid": sender_mid,
                    "incr_unread": False,
                    "talker_name": req.receiver_name,
                    "talker_avatar": req.receiver_avatar,
                    "relation": DmRelationEnum.NORMAL,
                },
            )
        ]
        if not filtered:
            session_rows.append(
                (
                    receiver_mid,
                    self._upsert_peer_row,
                    {
                        "msgkey": msgkey,
                        "preview": "[私信审核中]" if is_auditing else preview,
                        "msg_ts": msg_ts,
                        "sender_uid": sender_mid,
                        # 审核中：先不发未读红点，待管理端通过后再在 set_state 里补
                        "incr_unread": not is_auditing,
                        "talker_name": sender_name,
                        "talker_avatar": None,
                        "relation": (
                            DmRelationEnum.STRANGER
                            if is_stranger
                            else DmRelationEnum.NORMAL
                        ),
                    },
                )
            )
        session_rows.sort(key=lambda r: r[0])  # owner_mid 小者先行，统一锁顺序
        for _owner_mid, upsert_fn, params in session_rows:
            await upsert_fn(**params)
        await self.session.commit()

    async def _fallback_write_content(self, payload: DmContentPayload) -> None:
        """MQ 不可用时的降级：同步写分片，再失败则进死信表等待补偿。"""
        if settings.dm_content_sync_fallback and await DmContentService.write(payload):
            await mark_content_ready(self.session, payload.msgkey)
            logger.warning(f"MQ 不可用，msgkey={payload.msgkey} 已同步写入分片")
            return
        self.session.add(
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
        await self.session.commit()
        logger.error(f"msgkey={payload.msgkey} 正文写入失败，已进入死信表等待补偿")

    # ==================== 聊天记录 ====================

    async def fetch_messages(
        self,
        cursor: int | None = None,
        page_size: int | None = None,
    ) -> DmMessageListResp:
        """拉取本会话聊天记录（按 msgkey 游标倒序翻页）。

        读取分两步：先从主库拿索引（轻量、走联合索引），
        再按 msgkey 批量回捞分片里的正文。正文缺失时用摘要兜底。
        """
        owner_mid = self.owner_mid
        talker_mid = self.talker_mid
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
        rows = list((await self.session.exec(stmt)).all())
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
                    recalled_at=r.recalled_at,
                    recalled_by=r.recalled_by,
                )
            )

        # 进入会话即视为一次活跃行为
        await ActivityService.touch(self.session, owner_mid)

        # 进入会话即视为已读：打开聊天时一并清未读，避免「读了但红点不消失」。
        # msg_feed/unread 的 dm 未读取自各会话 unread_count，此处清零后顶部红点同步下降。
        # 仅确有未读才写回，避免无谓写库。
        sess_row = (
            await self.session.exec(
                select(DmSession).where(
                    DmSession.owner_mid == owner_mid,
                    DmSession.talker_mid == talker_mid,
                    DmSession.is_deleted == False,
                )
            )
        ).first()
        if sess_row is not None and sess_row.unread_count > 0:
            sess_row.unread_count = 0
            sess_row.ack_msgkey = sess_row.last_msgkey
            sess_row.updated_at = datetime.now()
            await self.session.commit()

        return DmMessageListResp(
            items=items,
            cursor=str(rows[-1].msgkey) if rows else None,
            has_more=has_more,
            talker_mid=talker_mid,
            session_key=self.session_key,
        )

    # ==================== 删除与撤回 ====================

    async def delete_messages(self, msgkeys: list[int]) -> int:
        """删除消息：只标记自己视角的索引行，对方仍能看到。"""
        if not msgkeys:
            return 0
        result = await self.session.exec(  # type: ignore[call-overload]
            update(DmMessageIndex)
            .where(
                col(DmMessageIndex.owner_mid) == self.owner_mid,
                col(DmMessageIndex.msgkey).in_(msgkeys),
            )
            .values(msg_status=DmMsgStatusEnum.DELETED, updated_at=datetime.now())
        )
        await self.session.commit()
        return int(getattr(result, "rowcount", 0) or 0)

    async def recall_message(self, msgkey: int) -> tuple[bool, str]:
        """撤回消息：双方均不可见，且物理抹掉分片里的正文。

        限制：只有发送者本人可撤回，且必须在 `dm_recall_window_seconds` 内。
        时间判定直接用 msgkey 内嵌的时间戳，无需回查数据库。
        """
        operator_mid = self.owner_mid
        row = (
            await self.session.exec(
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
        # 删除是单方面的：自己视角已删除（DELETED）的消息，撤回入口关闭，
        # 对方视角不受影响（仍可见，但对方非发送者本就不可撤回）。
        if row.msg_status is DmMsgStatusEnum.DELETED:
            return False, "消息已删除，无法撤回"
        if row.msg_status is DmMsgStatusEnum.RECALLED:
            return True, "消息已撤回"

        elapsed = (
            datetime.now().timestamp() * 1000 - parse_timestamp_ms(msgkey)
        ) / 1000
        if elapsed > settings.dm_recall_window_seconds:
            return False, f"超过 {settings.dm_recall_window_seconds} 秒的消息不可撤回"

        now = datetime.now()
        # 撤回是双向的：一次更新掉收发双方的索引行，并记录撤回操作者，
        # 双方聊天记录据此展示「你/对方撤回了一条消息」。
        await self.session.exec(  # type: ignore[call-overload]
            update(DmMessageIndex)
            .where(col(DmMessageIndex.msgkey) == msgkey)
            .values(
                msg_status=DmMsgStatusEnum.RECALLED,
                content_preview="[消息已撤回]",
                recalled_at=now,
                recalled_by=operator_mid,
                updated_at=now,
            )
        )
        # 会话列表的最后一条快照同步刷新
        await self.session.exec(  # type: ignore[call-overload]
            update(DmSession)
            .where(col(DmSession.last_msgkey) == msgkey)
            .values(last_content_preview="[消息已撤回]", updated_at=now)
        )
        await self.session.commit()

        await DmContentService.clear_content(msgkey)
        return True, "撤回成功"

    # ==================== 已读 / 删会话 ====================

    async def ack(self, ack_msgkey: int | None = None) -> int:
        """标记本会话已读：未读清零并抬高已读水位。"""
        row = await self.load()
        if row is None:
            return 0
        row.unread_count = 0
        if ack_msgkey is not None:
            row.ack_msgkey = max(row.ack_msgkey or 0, ack_msgkey)
        else:
            row.ack_msgkey = row.last_msgkey
        row.updated_at = datetime.now()
        self.session.add(row)
        await self.session.commit()
        return 1

    async def delete(self) -> int:
        """删除本会话（仅自己不可见）。"""
        row = await self.load()
        if row is None:
            return 0
        row.is_deleted = True
        row.unread_count = 0
        row.updated_at = datetime.now()
        self.session.add(row)
        await self.session.commit()
        return 1

    async def top(self, top: bool) -> tuple[int, int]:
        """置顶 / 取消置顶本会话（仅自己视角，owner_mid）。

        置顶唯一真相源为 `top_ts`（毫秒时间戳）：`top=True` 置顶写当前毫秒、
        取消置顶写 0。`is_top` 列同步写（兼容字段，读取不再依赖它）。
        幂等：会话不存在返回 `(0, 0)`；重复置顶刷新时间戳；未置顶取消 no-op。

        Returns:
            (affected, top_ts)：affected 为受影响行数（0=会话不存在），
            top_ts 为操作后的置顶时间戳。
        """
        row = await self.load()
        if row is None:
            return 0, 0
        now_ms = int(datetime.now().timestamp() * 1000)
        if top:
            row.top_ts = now_ms
            row.is_top = True
        else:
            row.top_ts = 0
            row.is_top = False
        row.updated_at = datetime.now()
        self.session.add(row)
        await self.session.commit()
        return 1, int(row.top_ts)

    # ==================== 内部方法 ====================

    async def _is_stranger(
        self, receiver_mid: int, sender_mid: int
    ) -> bool:
        """判断发送者对接收者而言是否为陌生人。

        判定规则（任一成立即为熟人）：
        1. 接收方已有与对方的会话且被标记为普通关系；
        2. 接收方曾经给对方发过消息（说明主动建立过联系）。
        """
        existing = await _get_session_row(self.session, receiver_mid, sender_mid)
        if existing is not None and existing.relation is DmRelationEnum.NORMAL:
            return False
        replied = (
            await self.session.exec(
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

    async def _check_daily_send_limit(self, sender_mid: int) -> None:
        """每日发送总量闸门：单用户当天作为发送者的私信数达到上限则拒绝。

        口径：统计 `owner_mid==sender AND sender_uid==sender`（发送方视角的索引行），
        `msg_ts` 落在本自然日窗口内。写扩散每封私信在发送方视角恒有一行，故不会重复计数；
        被陌生人过滤只留发送方视角的行同样计入「已发出」。上限由 `dm_daily_send_limit` 配置
        （0 表示不限制）。命中抛 `DmDailySendLimitError`（消息不落库）。
        """
        limit = settings.dm_daily_send_limit
        if limit <= 0:
            return
        now = datetime.now()
        day_start = datetime(now.year, now.month, now.day)
        start_ms = int(day_start.timestamp() * 1000)
        end_ms = start_ms + 24 * 60 * 60 * 1000
        sent = int(
            (
                await self.session.exec(
                    select(func.count())
                    .select_from(DmMessageIndex)
                    .where(
                        DmMessageIndex.owner_mid == sender_mid,
                        DmMessageIndex.sender_uid == sender_mid,
                        DmMessageIndex.msg_status != DmMsgStatusEnum.DELETED,
                        DmMessageIndex.msg_ts >= start_ms,
                        DmMessageIndex.msg_ts < end_ms,
                    )
                )
            ).one()
            or 0
        )
        if sent >= limit:
            logger.warning(
                f"用户 {sender_mid} 今日私信发送已达上限（{sent}/{limit}），本次发送被拒绝"
            )
            raise DmDailySendLimitError(limit)

    async def _check_stranger_gate(self, sender_mid: int, receiver_mid: int) -> None:
        """陌生人单条闸门：对方未关注我、且从未回过我消息时，我至多可发限内条数。

        判定（发送方 S → 接收方 R）：
        1. `R 是否关注 S`：查 `msg_user_follow`（`FollowService.is_following(R, S)`）；
        2. `R 是否回过 S`：`DmMessageIndex` 中 `owner==R AND sender_uid==R AND talker==S` 是否有行；
        3. 仅当「R 未关注 S 且 R 从未回过 S」时，再统计 `S` 已发给 `R` 的条数
           （`owner==S AND sender_uid==S AND talker==R`），达到 `dm_stranger_gate_limit`
           （默认 1）即拒绝；否则放行（本条成为对方回我前最后一条额度）。

        解除条件：一旦 R 关注了 S，或 R 给 S 回过任意一条消息，本方向即不再受限。
        开关 `dm_stranger_gate_enabled`，命中抛 `DmStrangerSendLimitError`（消息不落库）。
        """
        if not settings.dm_stranger_gate_enabled:
            return
        limit = settings.dm_stranger_gate_limit
        if limit <= 0:
            return
        # 对方已关注我 → 解除限制
        if await FollowService.is_following(self.session, receiver_mid, sender_mid):
            return
        # 对方曾回过我 → 解除限制
        replied = int(
            (
                await self.session.exec(
                    select(func.count())
                    .select_from(DmMessageIndex)
                    .where(
                        DmMessageIndex.owner_mid == receiver_mid,
                        DmMessageIndex.talker_mid == sender_mid,
                        DmMessageIndex.sender_uid == receiver_mid,
                        DmMessageIndex.msg_status != DmMsgStatusEnum.DELETED,
                    )
                )
            ).one()
            or 0
        )
        if replied > 0:
            return
        # 陌生人且对方从未回过：统计我已发条数
        sent = int(
            (
                await self.session.exec(
                    select(func.count())
                    .select_from(DmMessageIndex)
                    .where(
                        DmMessageIndex.owner_mid == sender_mid,
                        DmMessageIndex.talker_mid == receiver_mid,
                        DmMessageIndex.sender_uid == sender_mid,
                        DmMessageIndex.msg_status != DmMsgStatusEnum.DELETED,
                    )
                )
            ).one()
            or 0
        )
        if sent >= limit:
            logger.warning(
                f"用户 {sender_mid} 向陌生人 {receiver_mid} 连发私信触发单条闸门（已发 {sent} 条）"
            )
            raise DmStrangerSendLimitError(limit)

    async def _upsert_owner_row(
        self,
        *,
        msgkey: int,
        preview: str,
        msg_ts: int,
        sender_uid: int,
        incr_unread: bool,
        talker_name: str | None,
        talker_avatar: str | None,
        relation: DmRelationEnum,
    ) -> None:
        """更新（或创建）self.owner_mid 视角的会话行（主动发起方视角）。"""
        await self._upsert_row(
            owner_mid=self.owner_mid,
            talker_mid=self.talker_mid,
            msgkey=msgkey,
            preview=preview,
            msg_ts=msg_ts,
            sender_uid=sender_uid,
            incr_unread=incr_unread,
            talker_name=talker_name,
            talker_avatar=talker_avatar,
            relation=relation,
        )

    async def _upsert_peer_row(
        self,
        *,
        msgkey: int,
        preview: str,
        msg_ts: int,
        sender_uid: int,
        incr_unread: bool,
        talker_name: str | None,
        talker_avatar: str | None,
        relation: DmRelationEnum,
    ) -> None:
        """更新（或创建）self.talker_mid 视角的会话行（接收方视角）。"""
        await self._upsert_row(
            owner_mid=self.talker_mid,
            talker_mid=self.owner_mid,
            msgkey=msgkey,
            preview=preview,
            msg_ts=msg_ts,
            sender_uid=sender_uid,
            incr_unread=incr_unread,
            talker_name=talker_name,
            talker_avatar=talker_avatar,
            relation=relation,
        )

    async def _upsert_row(
        self,
        *,
        owner_mid: int,
        talker_mid: int,
        msgkey: int,
        preview: str,
        msg_ts: int,
        sender_uid: int,
        incr_unread: bool,
        talker_name: str | None,
        talker_avatar: str | None,
        relation: DmRelationEnum,
    ) -> None:
        """原子 upsert（INSERT ... ON DUPLICATE KEY UPDATE）某一方视角的会话行。

        原先是「先 SELECT 再 INSERT」的读改写：并发两条同一 (owner, talker) 私信会
        同时读到 None、都执行 INSERT，命中 uq_dm_session_owner_talker 抛 1062
        (Duplicate entry)，整条发送失败。改用 MySQL 原子 upsert 后，重复发送同一
        会话由数据库层去重（唯一键冲突即转 UPDATE），彻底消除并发竞态。

        注意：`ins` 必须持有 ``values()`` **之后**的实例，UPDATE 里的 ``ins.inserted``
        才与 ``ON DUPLICATE KEY UPDATE`` 内部持有的 ``inserted_alias`` 是同一对象
        （``on_duplicate_key_update`` 是 generative 方法，会把实例复制一份；
        若像 ``mysql_insert(t).values(...).on_duplicate_key_update(ins.inserted.x)``
        那样在**原始实例**上引用 ``inserted``，SQLAlchemy 2.0.51 在 MySQL 8.0.20+
        下无法把 ``inserted.x`` 替换为行别名 ``new.x``，生成 ``AS new ... inserted.x``
        的非法 SQL，MySQL 9.x 报 `Unknown column 'inserted.updated_at'`）。
        """
        now = datetime.now()
        ins = mysql_insert(DmSession.__table__).values(
            owner_mid=owner_mid,
            talker_mid=talker_mid,
            session_key=make_session_key(owner_mid, talker_mid),
            talker_name=talker_name,
            talker_avatar=talker_avatar,
            last_msgkey=msgkey,
            last_content_preview=preview,
            last_msg_ts=msg_ts,
            last_sender_uid=sender_uid,
            relation=relation.value,
            unread_count=1 if incr_unread else 0,
            is_deleted=False,
            created_at=now,
            updated_at=now,
        )
        stmt = ins.on_duplicate_key_update(
            # 仅当本次提供了姓名/头像才覆盖，避免把已有展示信息刷成 NULL
            talker_name=func.coalesce(ins.inserted.talker_name, DmSession.talker_name),
            talker_avatar=func.coalesce(
                ins.inserted.talker_avatar, DmSession.talker_avatar
            ),
            last_msgkey=ins.inserted.last_msgkey,
            last_content_preview=ins.inserted.last_content_preview,
            last_msg_ts=ins.inserted.last_msg_ts,
            last_sender_uid=ins.inserted.last_sender_uid,
            # 未读按需 +1；不增时保持原值
            unread_count=(
                DmSession.unread_count + 1 if incr_unread else DmSession.unread_count
            ),
            # 仅 STRANGER→NORMAL 升级；NORMAL 不会被改回 STRANGER
            relation=case(
                (
                    DmSession.relation == DmRelationEnum.STRANGER.value,
                    ins.inserted.relation,
                ),
                else_=DmSession.relation,
            ),
            is_deleted=False,
            updated_at=ins.inserted.updated_at,
        )
        await self.session.exec(stmt)  # type: ignore[call-overload]


# ==================== 收件箱（owner 级集合）====================


class DmInbox:
    """某用户的私信收件箱：会话集合 + 未读汇总。

    承载 owner 级批量操作（会话列表 / 未读总数），对应前端私信首页的红点与列表。
    """

    def __init__(self, session: AsyncSession, owner_mid: int) -> None:
        self.session = session
        self.owner_mid = owner_mid

    async def list_sessions(
        self,
        relation: DmRelationEnum | None = None,
        page_num: int = 1,
        page_size: int | None = None,
    ) -> DmSessionListResp:
        """会话列表。

        `relation` 用于把「陌生人消息」折叠成独立分组：
        传 NORMAL 得到主列表，传 STRANGER 得到陌生人列表，不传则全部。
        """
        page_size = page_size or settings.dm_default_page_size
        conditions = [
            DmSession.owner_mid == self.owner_mid,
            DmSession.is_deleted == False,
        ]
        if relation is not None:
            conditions.append(DmSession.relation == relation)

        total = int(
            (
                await self.session.exec(
                    select(func.count()).select_from(DmSession).where(*conditions)
                )
            ).one()
            or 0
        )
        stmt = (
            select(DmSession)
            .where(*conditions)
            # 置顶恒在前（top_ts DESC，最近置顶优先），再按最后消息时间倒序（2.59.0）
            .order_by(col(DmSession.top_ts).desc(), col(DmSession.last_msg_ts).desc())  # type: ignore[union-attr]
            .offset((page_num - 1) * page_size)
            .limit(page_size)
        )
        rows = (await self.session.exec(stmt)).all()

        # 批量解析对端用户卡片（昵称 + 头像）：DM 库与 pptr 库隔离，get_many 内部自建
        # 只读会话。一次 WHERE uid IN (...) 取回本页所有对端信息，避免每条会话各自查库；
        # 优先用实时卡片数据，缺漏时回落到会话快照里存储的 name/avatar。
        if rows:
            user_cache = await PptrUser.get_many([r.talker_mid for r in rows])
        else:
            user_cache = {}

        # 未读汇总：主列表红点与陌生人红点分开展示
        unread_stmt = (
            select(DmSession.relation, func.sum(DmSession.unread_count))
            .where(DmSession.owner_mid == self.owner_mid, DmSession.is_deleted == False)
            .group_by(DmSession.relation)
        )
        unread_map = {
            str(rel): int(cnt or 0)
            for rel, cnt in (await self.session.exec(unread_stmt)).all()
        }

        return DmSessionListResp(
            items=[_to_session_item(r, user_cache) for r in rows],
            total=total,
            unread_total=sum(unread_map.values()),
            stranger_unread=unread_map.get(str(DmRelationEnum.STRANGER), 0),
        )

    async def count_unread(self) -> int:
        """本用户私信未读总数。"""
        stmt = select(func.sum(DmSession.unread_count)).where(
            DmSession.owner_mid == self.owner_mid,
            DmSession.is_deleted == False,
        )
        return int((await self.session.exec(stmt)).one() or 0)


__all__ = [
    "DmSessionObject",
    "DmInbox",
    "make_session_key",
    "mark_content_ready",
    "retry_dead_letters",
]
