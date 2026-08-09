"""私信管理端服务（审核队列 / 上下架 / 统计）。

全部要求 `AdminUser`（role=root）。与评论审核（`CommentAdminService`）对齐：

- 审核队列：捞出处于 `auditing` / `rejected` / `hidden` 的私信，人工通过 / 驳回 / 下架 / 恢复。
- 写扩散：一条私信在 `msg_dm_index` 收发双方各一行，`audit_state` 同步双行，
  保证发送方与接收方视角一致。
- 可见性：被 `rejected` / `hidden` 的私信在 `DmService.list_messages` 已被过滤，
  对用户不可见；`pass` / `restore` 把状态拨回 `normal` 重新可见。
"""

from datetime import datetime

from loguru import logger
from sqlalchemy import func
from sqlmodel import col, select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.db import DmMessageIndex, DmSession
from app.models.enums import DmAuditStateEnum, NotifyLevelEnum
from app.models.schemas import (
    DmAuditItem,
    DmAuditListResp,
    DmSessionContextResp,
    DmStatsResp,
)
from app.services.comment import summarize_text
from app.services.dm import DmService
from app.services.notify import NotifyService
from app.utils.notify_markup import markup_inline_link
from app.services.pptr_user import PptrUserService
from app.utils.audit_source import build_dm_source


class DmAdminService:
    """私信管理端操作。"""

    # ==================== 审核 ====================

    # 被处置后在会话列表预览里展示的占位文案
    _PREVIEW_PLACEHOLDER = {
        DmAuditStateEnum.REJECTED: "[该消息已被管理员驳回]",
        DmAuditStateEnum.HIDDEN: "[该消息已被管理员下架]",
    }

    @staticmethod
    async def set_state(
        session: AsyncSession,
        msgkey: int,
        state: DmAuditStateEnum,
        note: str | None = None,
    ) -> bool:
        """人工设置一条私信的审核状态（通过 / 驳回 / 下架 / 恢复）。

        写扩散双行同步更新；状态翻转不调整计数（计数保持冻结）。
        驳回 / 下架时同步刷新双方会话列表的最后一条预览，
        让用户在私信列表里也能看到「该消息已被驳回 / 下架」。

        四种状态变更都会向发送者推送系统通知，正文带上「收件人 + 消息摘要」，
        `jump_url` 挂会话上下文地址，便于点通知直达那条私信。
        驳回 / 下架时把管理员填写的 `note` 作为处理原因写进正文。
        """
        rows = (
            await session.exec(
                select(DmMessageIndex).where(col(DmMessageIndex.msgkey) == msgkey)
            )
        ).all()
        if not rows:
            return False
        prev_state = rows[0].audit_state
        for r in rows:
            r.audit_state = state
            session.add(r)

        # 驳回 / 下架：把双方会话列表的最后一条预览替换为占位
        placeholder = DmAdminService._PREVIEW_PLACEHOLDER.get(state)
        if placeholder:
            for r in rows:
                await DmAdminService._sync_session_preview(
                    session, r.owner_mid, r.talker_mid, msgkey, placeholder
                )

        # 审核通过（auditing -> normal）：把接收方会话快照刷新为真实内容并补未读，
        # 使「先审后发」的私信在通过后对接收方正常浮现。
        if prev_state == DmAuditStateEnum.AUDITING and state == DmAuditStateEnum.NORMAL:
            receiver_row = next(
                (r for r in rows if r.owner_mid != r.sender_uid), None
            )
            if receiver_row is not None:
                sess = await DmService._get_session(
                    session, receiver_row.owner_mid, receiver_row.talker_mid
                )
                if sess is not None:
                    sess.last_msgkey = receiver_row.msgkey
                    sess.last_content_preview = receiver_row.content_preview
                    sess.last_msg_ts = receiver_row.msg_ts
                    sess.last_sender_uid = receiver_row.sender_uid
                    sess.unread_count += 1
                    sess.updated_at = datetime.now()
                    session.add(sess)

        await session.commit()

        # 状态实际变化时，向发送者推送系统通知（弱依赖：失败仅告警）
        if prev_state != state:
            sender_row = next((r for r in rows if r.owner_mid == r.sender_uid), rows[0])
            try:
                await DmAdminService._notify_state_changed(
                    sender_row, state, msgkey, note=note
                )
            except Exception as e:  # noqa: BLE001
                logger.warning(f"私信状态变更通知投递失败（弱依赖）: {e}")
        return True

    # 私信状态 → (通知标题, 正文首句, 通知级别)
    _NOTIFY_TEMPLATE: dict[DmAuditStateEnum, tuple[str, str, NotifyLevelEnum]] = {
        DmAuditStateEnum.NORMAL: (
            "私信审核通过",
            "已通过审核，对方现已可见。",
            NotifyLevelEnum.NORMAL,
        ),
        DmAuditStateEnum.AUDITING: (
            "私信审核中",
            "已被移入审核队列，通过后对方可见。",
            NotifyLevelEnum.NORMAL,
        ),
        DmAuditStateEnum.REJECTED: (
            "私信审核未通过",
            "未通过审核，已被驳回。",
            NotifyLevelEnum.IMPORTANT,
        ),
        DmAuditStateEnum.HIDDEN: (
            "私信已被下架",
            "已被管理员下架，当前对对方不可见。",
            NotifyLevelEnum.IMPORTANT,
        ),
    }

    @staticmethod
    async def _notify_state_changed(
        row: DmMessageIndex,
        state: DmAuditStateEnum,
        msgkey: int,
        note: str | None = None,
    ) -> None:
        """私信状态变更通知：与评论审核同构。

        只说「已通过审核」等于什么都没说——发送者同时可能有多条私信在审。
        正文统一带上「发给谁 + 消息摘要」，`jump_url` 挂会话上下文地址，
        让发送者能直接点回那条私信所在的会话。
        驳回 / 下架额外把管理员填写的 `note` 作为处理原因写进正文。
        """
        template = DmAdminService._NOTIFY_TEMPLATE.get(state)
        if template is None:
            return
        title, summary, level = template

        source = build_dm_source(
            row.session_key, row.sender_uid, row.talker_mid, msgkey
        )
        target_link = markup_inline_link(f"用户 {row.talker_mid}", source.url)
        lines = [f"您发送给{target_link}的私信{summary}"]
        if row.content_preview:
            lines.append(f"私信内容：{summarize_text(row.content_preview)}")
        if note and (state in (DmAuditStateEnum.REJECTED, DmAuditStateEnum.HIDDEN)):
            lines.append(f"处理原因：{note}")

        await NotifyService.send_to_user(
            row.sender_uid,
            title=title,
            content="\n".join(lines),
            level=level,
            jump_url=source.url,
        )

    @staticmethod
    async def bulk_set_state(
        session: AsyncSession,
        msgkeys: list[int],
        state: DmAuditStateEnum,
        note: str | None = None,
        notes: dict[str, str] | None = None,
    ) -> tuple[int, list[int]]:
        """批量设置私信审核状态。

        逐条原因优先使用 `notes[msgkey字符串]`；未提供则该条回落到统一 `note`。

        Returns:
            `(success_count, failed_msgkeys)` —— failed 为不存在或处理失败的 msgkey。
        """
        success = 0
        failed: list[int] = []
        for msgkey in msgkeys:
            try:
                per_note = (notes or {}).get(str(msgkey)) if notes is not None else note
                if await DmAdminService.set_state(session, msgkey, state, note=per_note):
                    success += 1
                else:
                    failed.append(msgkey)
            except Exception as e:  # noqa: BLE001
                logger.warning(f"批量私信审核失败 msgkey={msgkey}: {e}")
                failed.append(msgkey)
        return success, failed

    @staticmethod
    async def _sync_session_preview(
        session: AsyncSession,
        owner_mid: int,
        talker_mid: int,
        msgkey: int,
        text: str,
    ) -> None:
        """仅当该会话的最后一条消息正是被处置的这条时，更新预览占位。"""
        sess = await DmService._get_session(session, owner_mid, talker_mid)
        if sess is not None and sess.last_msgkey == msgkey:
            sess.last_content_preview = text
            sess.updated_at = datetime.now()
            session.add(sess)

    @staticmethod
    async def list_audit_queue(
        session: AsyncSession,
        states: list[DmAuditStateEnum],
        page_num: int = 1,
        page_size: int = 20,
    ) -> tuple[list[DmAuditItem], int]:
        """审核队列：处于指定审核状态的私信（按 msgkey 去重写扩散双行）。"""
        total = int(
            (
                await session.exec(
                    select(func.count())
                    .select_from(DmMessageIndex)
                    .where(col(DmMessageIndex.audit_state).in_(states))
                )
            ).one()
            or 0
        )
        rows = (
            await session.exec(
                select(DmMessageIndex)
                .where(col(DmMessageIndex.audit_state).in_(states))
                .order_by(col(DmMessageIndex.msgkey).desc())
                .offset((page_num - 1) * page_size)
                .limit(page_size)
            )
        ).all()

        # 写扩散双行去重（同一 msgkey 收发双方各一行，状态始终一致）
        seen: dict[int, DmMessageIndex] = {}
        for r in rows:
            seen.setdefault(r.msgkey, r)

        items = [DmAdminService._to_item(r) for r in seen.values()]
        await DmAdminService._fill_senders(items)
        return items, total

    @staticmethod
    async def get_audit_item(
        session: AsyncSession, msgkey: int
    ) -> DmAuditItem | None:
        """按 msgkey 构造一条审核项（含摘要），用于审核后回显。"""
        row = (
            await session.exec(
                select(DmMessageIndex).where(col(DmMessageIndex.msgkey) == msgkey)
            )
        ).first()
        item = DmAdminService._to_item(row) if row else None
        if item is not None:
            await DmAdminService._fill_senders([item])
        return item

    @staticmethod
    def _to_item(r: DmMessageIndex) -> DmAuditItem:
        # 写扩散双行里 owner/talker 视角不同，接收方 mid 统一取「非发送者」的一方
        receiver_mid = r.talker_mid if r.sender_uid == r.owner_mid else r.owner_mid
        return DmAuditItem(
            msgkey=str(r.msgkey),
            sender_mid=r.sender_uid,
            talker_mid=receiver_mid,
            session_key=r.session_key,
            message=r.content_preview or "",
            msg_type=r.msg_type,
            audit_state=r.audit_state,
            msg_ts=r.msg_ts,
            content_ready=r.content_ready,
            created_at=r.created_at,
            source=build_dm_source(
                r.session_key, r.sender_uid, receiver_mid, r.msgkey
            ),
        )

    @staticmethod
    async def _fill_senders(items: list[DmAuditItem]) -> None:
        """批量把每条私信的发送者信息（直连 pptr）填进 `sender` 字段。"""
        if not items:
            return
        profiles = await PptrUserService.get_many([it.sender_mid for it in items])
        for it in items:
            it.sender = profiles.get(it.sender_mid)

    # ==================== 内容来源（会话上下文）====================

    @staticmethod
    async def get_session_context(
        session: AsyncSession,
        session_key: str | None = None,
        msgkey: int | None = None,
        page_size: int = 30,
    ) -> DmSessionContextResp | None:
        """按 session_key（或 msgkey 反查会话）拉取该会话的消息上下文。

        管理端点击「内容来源」时使用：审核队列里只有孤立的一条消息，
        没有上下文根本判断不了语境，这里把整段会话按 msgkey 倒序回捞。
        正文一律取索引行的 `content_preview`（与审核队列口径一致，不读分片）。
        """
        anchor: DmMessageIndex | None = None
        if msgkey:
            anchor = (
                await session.exec(
                    select(DmMessageIndex).where(col(DmMessageIndex.msgkey) == msgkey)
                )
            ).first()
            if anchor is None and not session_key:
                return None
            if anchor is not None:
                session_key = anchor.session_key
        if not session_key:
            return None

        total = int(
            (
                await session.exec(
                    select(func.count(func.distinct(col(DmMessageIndex.msgkey))))
                    .select_from(DmMessageIndex)
                    .where(col(DmMessageIndex.session_key) == session_key)
                )
            ).one()
            or 0
        )
        rows = (
            await session.exec(
                select(DmMessageIndex)
                .where(col(DmMessageIndex.session_key) == session_key)
                .order_by(col(DmMessageIndex.msgkey).desc())
                # 写扩散双行，取两倍再去重，保证去重后仍有 page_size 条
                .limit(page_size * 2)
            )
        ).all()

        seen: dict[int, DmMessageIndex] = {}
        for r in rows:
            seen.setdefault(r.msgkey, r)
        items = [DmAdminService._to_item(r) for r in list(seen.values())[:page_size]]
        await DmAdminService._fill_senders(items)

        ref = anchor or (rows[0] if rows else None)
        if ref is None:
            return DmSessionContextResp(session_key=session_key, items=[], total=total)
        receiver_mid = (
            ref.talker_mid if ref.sender_uid == ref.owner_mid else ref.owner_mid
        )
        return DmSessionContextResp(
            session_key=session_key,
            sender_mid=ref.sender_uid,
            talker_mid=receiver_mid,
            source=build_dm_source(
                session_key, ref.sender_uid, receiver_mid, ref.msgkey
            ),
            items=items,
            total=total,
        )

    # ==================== 统计 ====================

    @staticmethod
    async def get_stats(session: AsyncSession) -> DmStatsResp:
        """私信全局统计（管理端低频查询）。"""
        subq = select(DmMessageIndex.msgkey).distinct().subquery()
        total_dm = int(
            (await session.exec(select(func.count()).select_from(subq))).one() or 0
        )
        today_start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        today_new = int(
            (
                await session.exec(
                    select(func.count())
                    .select_from(DmMessageIndex)
                    .where(col(DmMessageIndex.created_at) >= today_start)
                )
            ).one()
            or 0
        )

        async def _count(state: DmAuditStateEnum) -> int:
            return int(
                (
                    await session.exec(
                        select(func.count())
                        .select_from(DmMessageIndex)
                        .where(col(DmMessageIndex.audit_state) == state)
                    )
                ).one()
                or 0
            )

        return DmStatsResp(
            total_dm=total_dm,
            today_new=today_new,
            auditing=await _count(DmAuditStateEnum.AUDITING),
            rejected=await _count(DmAuditStateEnum.REJECTED),
            hidden=await _count(DmAuditStateEnum.HIDDEN),
        )


__all__ = ["DmAdminService"]
