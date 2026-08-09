"""评论管理端服务（Phase 5.3，reply-admin）。

管理端能力，全部要求 `AdminUser`：

- 审核队列：捞出处于 `auditing` / `rejected` 的评论，人工通过 / 驳回 / 下架 / 恢复。
- 明文 IP：普通接口出参已打码（D3），管理端可看原始 IPv4 / IPv6，用于安全溯源。
- 全局统计：评论总数 / 趋势 / 用户排行（管理端低频查询，允许聚合 `COUNT`）。

审核通过即把状态拨回 `normal` 对外可见；驳回 / 下架置 `rejected` / `hidden`；
恢复则把被下架 / 驳回的评论重新置 `normal`。
"""

from datetime import datetime

from loguru import logger
from sqlalchemy import func
from sqlmodel import col, select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.db import CommentContent, CommentIndex, CommentSubject
from app.models.enums import CommentStateEnum, CommentTypeEnum, NotifyLevelEnum
from app.models.schemas import (
    CommentAuditItem,
    CommentSourceResp,
    CommentStatsResp,
    CommentUserBrief,
)
from app.services.comment import (
    DEFAULT_REJECT_REASON,
    CommentService,
    summarize_text,
)
from app.services.notify import NotifyService
from app.services.pptr_user import PptrUserService
from app.utils.audit_source import build_comment_source
from app.utils.notify_markup import markup_inline_link

# 管理端下架未填写原因时的兜底文案
DEFAULT_HIDDEN_REASON = "评论内容违反社区规范"


class CommentAdminService:
    """评论管理端操作。"""

    # ==================== 审核 ====================

    @staticmethod
    async def set_state(
        session: AsyncSession,
        rpid: int,
        state: CommentStateEnum,
        note: str | None = None,
        operator_mid: int = 0,
    ) -> bool:
        """人工设置一条评论的状态（通过 / 驳回 / 下架 / 恢复）。

        注意：状态翻转不调整冗余计数（计数保持冻结），计数漂移由
        Phase 5.10 的对账定时任务统一校准。

        四种状态变更都会向作者发送系统通知，正文统一带上「来源评论区 + 原文摘要」，
        并把可跳转地址挂到 `jump_url`；其中 REJECTED（审核未通过）/ HIDDEN（下架）
        额外把管理员填写的 `note` 作为处理原因写进正文。
        """
        row = (
            await session.exec(
                select(CommentIndex).where(col(CommentIndex.rpid) == rpid)
            )
        ).one_or_none()
        if row is None:
            return False
        prev_state = row.state
        row.state = state
        session.add(row)
        await session.commit()

        # 状态实际变化时，按变更类型向作者推送系统通知（弱依赖：失败仅告警）
        if prev_state != state:
            try:
                if state == CommentStateEnum.NORMAL:
                    # 审核通过 / 恢复：对外可见
                    await CommentAdminService._notify_state_changed(
                        session,
                        rpid,
                        row,
                        title="评论审核通过",
                        summary="发布的评论已通过审核，现已对外公开展示。",
                        operator_mid=operator_mid,
                    )
                elif state == CommentStateEnum.AUDITING:
                    # 被打回审核：对外不可见，等待复审
                    await CommentAdminService._notify_state_changed(
                        session,
                        rpid,
                        row,
                        title="评论审核中",
                        summary="发布的评论已被移入审核队列，通过后将对外公开展示。",
                        operator_mid=operator_mid,
                    )
                elif state == CommentStateEnum.REJECTED:
                    await CommentService.notify_audit_rejected(
                        row.mid,
                        rpid,
                        row.oid,
                        row.type,
                        reason=note or DEFAULT_REJECT_REASON,
                        creator_mid=operator_mid,
                        excerpt=await CommentAdminService._load_excerpt(session, rpid),
                    )
                elif state == CommentStateEnum.HIDDEN:
                    await CommentAdminService._notify_hidden(
                        session,
                        rpid,
                        row,
                        note=note,
                        operator_mid=operator_mid,
                    )
            except Exception as e:  # noqa: BLE001
                logger.warning(f"评论状态变更通知投递失败（弱依赖）: {e}")
        return True

    @staticmethod
    async def _load_excerpt(session: AsyncSession, rpid: int) -> str | None:
        """取评论原文，用于在通知里回显"被处理的是哪条内容"。"""
        content = (
            await session.exec(
                select(CommentContent.message).where(
                    col(CommentContent.rpid) == rpid
                )
            )
        ).one_or_none()
        return content

    @staticmethod
    async def _notify_state_changed(
        session: AsyncSession,
        rpid: int,
        row: CommentIndex,
        title: str,
        summary: str,
        operator_mid: int,
    ) -> None:
        """通过 / 打回审核的通知：与驳回、下架同构。

        通知不能只有一句「已通过审核」这种空话——作者往往同时有多条评论在审，
        必须让他一眼看出「是哪个评论区的哪条内容」，并能直接点回原处。
        因此正文同样带上来源与原文摘要，`jump_url` 挂可跳转地址。
        """
        source = build_comment_source(row.oid, row.type, rpid)
        excerpt = await CommentAdminService._load_excerpt(session, rpid)
        source_link = markup_inline_link(source.label, source.url or source.external_url)

        lines = [f"您在{source_link}{summary}"]
        if excerpt:
            lines.append(f"评论内容：{summarize_text(excerpt)}")

        await NotifyService.send_to_user(
            row.mid,
            title=title,
            content="\n".join(lines),
            jump_url=source.url or source.external_url,
            creator_mid=operator_mid,
        )

    @staticmethod
    async def _notify_hidden(
        session: AsyncSession,
        rpid: int,
        row: CommentIndex,
        note: str | None,
        operator_mid: int,
    ) -> None:
        """下架通知：与驳回同构，同样告知来源、原文与原因。"""
        source = build_comment_source(row.oid, row.type, rpid)
        excerpt = await CommentAdminService._load_excerpt(session, rpid)
        source_link = markup_inline_link(source.label, source.url or source.external_url)

        lines = [
            f"您在{source_link}发布的评论已被管理员下架，当前对所有用户不可见。",
        ]
        if excerpt:
            lines.append(f"评论内容：{summarize_text(excerpt)}")
        lines.append(f"下架原因：{note or DEFAULT_HIDDEN_REASON}")

        await NotifyService.send_to_user(
            row.mid,
            title="评论已被下架",
            content="\n".join(lines),
            level=NotifyLevelEnum.IMPORTANT,
            jump_url=source.url or source.external_url,
            creator_mid=operator_mid,
        )

    @staticmethod
    async def bulk_set_state(
        session: AsyncSession,
        rpids: list[int],
        state: CommentStateEnum,
        note: str | None = None,
        notes: dict[str, str] | None = None,
        operator_mid: int = 0,
    ) -> tuple[int, list[int]]:
        """批量设置评论状态。

        逐条原因优先使用 `notes[rpid字符串]`；未提供则该条回落到统一 `note`。

        Returns:
            `(success_count, failed_rpids)` —— failed 为不存在或处理失败的 rpid。
        """
        success = 0
        failed: list[int] = []
        for rpid in rpids:
            try:
                per_note = (notes or {}).get(str(rpid)) if notes is not None else note
                if await CommentAdminService.set_state(
                    session, rpid, state, note=per_note, operator_mid=operator_mid
                ):
                    success += 1
                else:
                    failed.append(rpid)
            except Exception as e:  # noqa: BLE001
                logger.warning(f"批量评论审核失败 rpid={rpid}: {e}")
                failed.append(rpid)
        return success, failed

    @staticmethod
    async def list_audit_queue(
        session: AsyncSession,
        states: list[CommentStateEnum] | None = None,
        page_num: int = 1,
        page_size: int = 20,
    ) -> tuple[list[CommentAuditItem], int]:
        """审核队列：按状态过滤的评论列表。

        `states` 由接口层根据调用者身份决定（root 可传任意状态，
        普通管理员被强制收敛为 `auditing`），此处不做鉴权。
        """
        states = states or [CommentStateEnum.AUDITING, CommentStateEnum.REJECTED]
        total = int(
            (
                await session.exec(
                    select(func.count())
                    .select_from(CommentIndex)
                    .where(col(CommentIndex.state).in_(states))
                )
            ).one()
            or 0
        )
        rows = (
            await session.exec(
                select(CommentIndex)
                .where(col(CommentIndex.state).in_(states))
                .order_by(col(CommentIndex.rpid).desc())
                .offset((page_num - 1) * page_size)
                .limit(page_size)
            )
        ).all()
        if not rows:
            return [], total

        rpids = [r.rpid for r in rows]
        contents = {
            c.rpid: c
            for c in (
                await session.exec(
                    select(CommentContent).where(
                        col(CommentContent.rpid).in_(rpids)
                    )
                )
            ).all()
        }
        # 批量取评论区 UP 主（内容来源展示），一次 IN 查询避免 N+1
        up_mids = await CommentAdminService._get_up_mids(
            session, {(r.oid, r.type) for r in rows}
        )
        # 作者信息：直连 pptr 一次 IN 查询取回，避免前端再回查
        profiles = await PptrUserService.get_many([r.mid for r in rows])
        items = [
            CommentAuditItem(
                rpid=str(r.rpid),
                oid=str(r.oid),
                type=r.type,
                mid=r.mid,
                message=contents[r.rpid].message if r.rpid in contents else "",
                state=r.state,
                like_count=r.like_count,
                ctime=r.created_at,
                ip_v4=contents[r.rpid].ip_v4 if r.rpid in contents else None,
                ip_v6=contents[r.rpid].ip_v6 if r.rpid in contents else None,
                plat=contents[r.rpid].plat if r.rpid in contents else None,
                device=contents[r.rpid].device if r.rpid in contents else None,
                source=build_comment_source(
                    r.oid, r.type, r.rpid, up_mids.get((r.oid, r.type))
                ),
                member=profiles.get(r.mid),
            )
            for r in rows
        ]
        return items, total

    @staticmethod
    async def _get_up_mids(
        session: AsyncSession, keys: set[tuple[int, CommentTypeEnum]]
    ) -> dict[tuple[int, CommentTypeEnum], int]:
        """批量取 `(oid, type)` 对应评论区的 UP 主 mid。"""
        if not keys:
            return {}
        oids = {oid for oid, _ in keys}
        rows = (
            await session.exec(
                select(CommentSubject).where(col(CommentSubject.oid).in_(oids))
            )
        ).all()
        return {(s.oid, s.type): s.up_mid for s in rows if (s.oid, s.type) in keys}

    @staticmethod
    async def get_audit_item(
        session: AsyncSession, rpid: int
    ) -> CommentAuditItem | None:
        """按 rpid 构造一条审核项（含明文 IP），用于审核后回显。"""
        row = (
            await session.exec(
                select(CommentIndex).where(col(CommentIndex.rpid) == rpid)
            )
        ).one_or_none()
        if row is None:
            return None
        content = (
            await session.exec(
                select(CommentContent).where(col(CommentContent.rpid) == rpid)
            )
        ).one_or_none()
        subject = await CommentAdminService._get_subject(session, row.oid, row.type)
        # 作者信息：直连 pptr 只读取回
        profile = await PptrUserService.get_many([row.mid])
        return CommentAuditItem(
            rpid=str(row.rpid),
            oid=str(row.oid),
            type=row.type,
            mid=row.mid,
            message=content.message if content else "",
            state=row.state,
            like_count=row.like_count,
            ctime=row.created_at,
            ip_v4=content.ip_v4 if content else None,
            ip_v6=content.ip_v6 if content else None,
            source=build_comment_source(
                row.oid, row.type, row.rpid, subject.up_mid if subject else None
            ),
            member=profile.get(row.mid),
        )

    # ==================== 内容来源 ====================

    @staticmethod
    async def _get_subject(
        session: AsyncSession, oid: int, type_: CommentTypeEnum
    ) -> CommentSubject | None:
        return (
            await session.exec(
                select(CommentSubject).where(
                    col(CommentSubject.oid) == oid,
                    col(CommentSubject.type) == type_,
                )
            )
        ).one_or_none()

    @staticmethod
    async def get_source(
        session: AsyncSession, rpid: int
    ) -> CommentSourceResp | None:
        """按 rpid 返回内容来源详情（评论区归属 + 跳转地址 + 评论区计数）。"""
        row = (
            await session.exec(
                select(CommentIndex).where(col(CommentIndex.rpid) == rpid)
            )
        ).one_or_none()
        if row is None:
            return None
        subject = await CommentAdminService._get_subject(session, row.oid, row.type)
        return CommentSourceResp(
            rpid=str(row.rpid),
            root=str(row.root),
            parent=str(row.parent),
            state=row.state,
            source=build_comment_source(
                row.oid, row.type, row.rpid, subject.up_mid if subject else None
            ),
            subject_state=subject.state if subject else None,
            root_count=subject.root_count if subject else 0,
            all_count=subject.all_count if subject else 0,
        )

    # ==================== 明文 IP（D3 管理端）====================

    @staticmethod
    async def get_plaintext_ip(
        session: AsyncSession, rpid: int
    ) -> tuple[str | None, str | None]:
        """返回原始 IPv4 / IPv6（仅管理端调用）。"""
        content = (
            await session.exec(
                select(CommentContent).where(col(CommentContent.rpid) == rpid)
            )
        ).one_or_none()
        if content is None:
            return None, None
        return content.ip_v4, content.ip_v6

    # ==================== 统计（Phase 5.8）====================

    @staticmethod
    async def get_stats(session: AsyncSession) -> CommentStatsResp:
        """评论区全局统计（管理端低频查询，允许聚合 COUNT）。

        计数口径：直接对 `CommentIndex` 按 `state` 分组 COUNT，覆盖 normal /
        auditing / rejected / hidden / deleted 全部状态——不再依赖 `CommentSubject`
        的冗余计数（后者只在 normal 时累加，会漏掉驳回 / 审核中等状态）。
        `total_comments` 取「非 deleted」各状态之和，确保驳回评论被计入总数。
        """
        # 各状态评论数：按 state 分组聚合
        state_rows = (
            await session.exec(
                select(CommentIndex.state, func.count())
                .select_from(CommentIndex)
                .group_by(CommentIndex.state)
            )
        ).all()
        state_counts: dict[str, int] = {s.value: 0 for s in CommentStateEnum}
        for st, cnt in state_rows:
            state_counts[st.value] = int(cnt)

        # 总数 = 全部状态之和减去已删除（deleted 视为移除，不计入在册评论）
        total_comments = sum(
            v for k, v in state_counts.items() if k != CommentStateEnum.DELETED.value
        )

        total_subjects = int(
            (await session.exec(select(func.count()).select_from(CommentSubject))).one()
            or 0
        )
        total_root = int(
            (
                await session.exec(
                    select(func.count())
                    .select_from(CommentIndex)
                    .where(col(CommentIndex.root) == 0)
                )
            ).one()
            or 0
        )

        today_start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        today_new = int(
            (
                await session.exec(
                    select(func.count())
                    .select_from(CommentIndex)
                    .where(col(CommentIndex.created_at) >= today_start)
                )
            ).one()
            or 0
        )

        # 用户评论排行（按发布条数 TOP10，含全部状态）
        top = (
            await session.exec(
                select(CommentIndex.mid, func.count())
                .where(col(CommentIndex.root) == 0)
                .group_by(CommentIndex.mid)
                .order_by(func.count().desc())
                .limit(10)
            )
        ).all()
        mids = [m for m, _ in top]
        # 作者展示信息从 pptr Postgres 只读取回（本服务不再冗余快照）
        profiles = await PptrUserService.get_many(mids)
        top_authors = [profiles[m] for m in mids if m in profiles]

        return CommentStatsResp(
            total_comments=total_comments,
            total_root=total_root,
            total_subjects=total_subjects,
            today_new=today_new,
            top_authors=top_authors,
            state_counts=state_counts,
        )


__all__ = ["CommentAdminService"]
