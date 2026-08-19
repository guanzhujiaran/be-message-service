"""动态审核服务（Phase 6）。

覆盖 P6-T1 ~ P6-T4：

- 管理员待审核列表（P6-T1）：auditStatus=auditing 按创建时间倒序分页。
- 审核通过（P6-T2）：auditStatus→normal + pubTime=now()；写 TMomentAuditLog；
  **不发通知**；dynType=FORWARD 时源动态 repostCount 原子 +1（状态机触发点①）。
- 审核驳回（P6-T3）：auditStatus→rejected + 写 auditRejectReason；写 TMomentAuditLog；
  **发驳回事件通知给作者**（EventTypeEnum.AUDIT_REJECT）；FORWARD ∧ before=normal 时
  源动态 repostCount 原子 -1（状态机触发点②）。
- 审核记录流水查询（P6-T4）：按 dynId / 操作员 / 时间段过滤。

作者信息（昵称 / 头像）经 ``PptrUserService.get_many`` 只读回查（与评论 / Feed 一致，
不冗余用户快照）。审核操作均为管理员行为，operatorRole 记为 ``admin``。
"""

from datetime import datetime

from loguru import logger
from sqlmodel import col, func, select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.db import TMoment, TMomentAuditLog
from app.models.enums import (
    EventTypeEnum,
    MomentAuditLogActionEnum,
    MomentAuditLogOperatorRoleEnum,
    MomentAuditStatusEnum,
    MomentTypeEnum,
    SourceTypeEnum,
)
from app.models.schemas.moment import (
    MomentAuditDetailResp,
    MomentAuditItem,
    MomentAuditListResp,
    MomentAuditLogItem,
    MomentAuditLogListResp,
)
from app.services.moment_stat import MomentStatService
from app.services.pptr_user import PptrUserService


def _iso(dt: datetime | None) -> str | None:
    """datetime → ISO 字符串（与现有服务一致，无 tz）。"""
    return dt.isoformat() if dt else None


async def _get_any(session: AsyncSession, moment_id: int) -> TMoment | None:
    """按 dynId 取动态（含全部状态，供审核后台查看；不按 normal / 软删过滤）。"""
    return (
        await session.exec(select(TMoment).where(col(TMoment.dynId) == moment_id))
    ).one_or_none()


def _to_audit_item(dyn: TMoment, author) -> MomentAuditItem:
    """TMoment → 审核队列卡片（author 来自 PptrUserService.get_many 结果）。"""
    return MomentAuditItem(
        dynId=dyn.dynId,
        dynIdStr=str(dyn.dynId),
        mid=dyn.mid,
        authorName=author.uname if author else None,
        authorFace=author.avatar if author else None,
        dynType=dyn.dynType.name,
        contentText=dyn.contentText,
        pubTime=_iso(dyn.pubTime),
        createdTime=_iso(dyn.created_at),
        auditStatus=dyn.auditStatus.value,
        isTop=dyn.isTop,
        topicId=dyn.topicId,
    )


def _to_audit_log_item(log: TMomentAuditLog) -> MomentAuditLogItem:
    """TMomentAuditLog → 流水卡片（枚举转字符串）。"""
    return MomentAuditLogItem(
        pk=log.pk,
        dynId=log.dynId,
        operatorMid=log.operatorMid,
        operatorRole=log.operatorRole.value,
        fromStatus=log.fromStatus.value if log.fromStatus is not None else None,
        toStatus=log.toStatus.value,
        actionType=log.actionType.value,
        rejectReason=log.rejectReason,
        remark=log.remark,
        createdTime=_iso(log.created_at),
    )


def _build_audit_log(
    *,
    moment_id: int,
    operator_mid: int,
    to_status: MomentAuditStatusEnum,
    action: MomentAuditLogActionEnum,
    from_status: MomentAuditStatusEnum | None = None,
    reject_reason: str | None = None,
    remark: str | None = None,
) -> TMomentAuditLog:
    """构造一条管理员审核流转记录。"""
    return TMomentAuditLog(
        dynId=moment_id,
        operatorMid=operator_mid,
        operatorRole=MomentAuditLogOperatorRoleEnum.ADMIN,
        fromStatus=from_status,
        toStatus=to_status,
        actionType=action,
        rejectReason=reject_reason,
        remark=remark,
    )


async def _notify_reject(
    operator_mid: int, dyn: TMoment, reject_reason: str
) -> None:
    """弱依赖：审核驳回事件通知作者（AUDIT_REJECT）。

    独立会话投递：即便事件落库失败，也绝不回滚审核主事务。
    """
    from app.services.event import EventService

    try:
        async with _new_session() as ns:
            await EventService.report(
                ns,
                _EventReportReq_for_reject(operator_mid, dyn, reject_reason),
            )
    except Exception as e:  # noqa: BLE001
        logger.warning(f"审核驳回通知投递失败（弱依赖，已忽略）: {e}")


def _EventReportReq_for_reject(operator_mid: int, dyn: TMoment, reject_reason: str):
    from app.models.schemas import EventReportReq

    return EventReportReq(
        mid=dyn.mid,
        event_type=EventTypeEnum.AUDIT_REJECT,
        source_type=SourceTypeEnum.DYNAMIC,
        source_id=str(dyn.dynId),
        actor_mid=operator_mid,
        content=reject_reason,
        biz_id=str(dyn.dynId),
    )


def _new_session():
    """取 be-message 主库独立会话（事件落库用，避免在审核事务内提交事件）。"""
    from app.core.database import new_session

    return new_session()


class MomentAuditService:
    """动态审核服务（静态方法集合，无状态）。"""

    # ==================== 待审核列表（P6-T1）====================

    @staticmethod
    async def pending_list(
        session: AsyncSession,
        *,
        page_num: int = 1,
        page_size: int = 20,
    ) -> MomentAuditListResp:
        """管理员待审核列表：auditStatus=auditing，按创建时间倒序，分页。"""
        page_num = max(1, page_num)
        page_size = min(max(1, page_size), 50)

        total = int(
            (
                await session.exec(
                    select(func.count())
                    .select_from(TMoment)
                    .where(col(TMoment.auditStatus) == MomentAuditStatusEnum.AUDITING)
                )
            ).one()
            or 0
        )
        rows = (
            await session.exec(
                select(TMoment)
                .where(col(TMoment.auditStatus) == MomentAuditStatusEnum.AUDITING)
                .order_by(col(TMoment.created_at).desc())
                .offset((page_num - 1) * page_size)
                .limit(page_size)
            )
        ).all()

        mids = {r.mid for r in rows}
        briefs = await PptrUserService.get_many(list(mids))
        items = [_to_audit_item(r, briefs.get(r.mid)) for r in rows]
        return MomentAuditListResp(
            items=items, total=total, page_num=page_num, page_size=page_size
        )

    # ==================== 审核通过（P6-T2）====================

    @staticmethod
    async def approve(
        session: AsyncSession,
        moment_id: int,
        *,
        operator_mid: int,
        remark: str | None = None,
    ) -> MomentAuditItem:
        """审核通过：auditStatus→normal + pubTime=now()；写 AuditLog；FORWARD 时源动态 +1。

        **不发通知**（与计划书一致）。
        """
        dyn = await _get_any(session, moment_id)
        if dyn is None:
            raise ValueError("动态不存在")
        from_status = dyn.auditStatus
        now = datetime.now()
        dyn.auditStatus = MomentAuditStatusEnum.NORMAL
        dyn.pubTime = now
        dyn.updated_at = now

        # 状态机触发点①：FORWARD 且源动态存在 → 源动态 repostCount +1
        if dyn.dynType is MomentTypeEnum.FORWARD and dyn.repostSrcDynId:
            await MomentStatService.incr_repost_count(session, dyn.repostSrcDynId, 1)

        session.add(
            _build_audit_log(
                moment_id=moment_id,
                operator_mid=operator_mid,
                to_status=MomentAuditStatusEnum.NORMAL,
                action=MomentAuditLogActionEnum.APPROVE,
                from_status=from_status,
                remark=remark,
            )
        )
        await session.commit()
        await session.refresh(dyn)
        logger.info(f"管理员 {operator_mid} 审核通过动态 dynId={moment_id}")
        briefs = await PptrUserService.get_many([dyn.mid])
        return _to_audit_item(dyn, briefs.get(dyn.mid))

    # ==================== 审核驳回（P6-T3）====================

    @staticmethod
    async def reject(
        session: AsyncSession,
        moment_id: int,
        *,
        operator_mid: int,
        reject_reason: str,
        remark: str | None = None,
    ) -> MomentAuditItem:
        """审核驳回：auditStatus→rejected + 写 auditRejectReason；写 AuditLog；
        发驳回事件通知给作者（AUDIT_REJECT）；FORWARD ∧ before=normal 时源动态 -1。"""
        dyn = await _get_any(session, moment_id)
        if dyn is None:
            raise ValueError("动态不存在")
        from_status = dyn.auditStatus
        before_normal = from_status == MomentAuditStatusEnum.NORMAL
        now = datetime.now()
        dyn.auditStatus = MomentAuditStatusEnum.REJECTED
        dyn.auditRejectReason = reject_reason
        dyn.updated_at = now

        # 状态机触发点②：FORWARD ∧ before=normal → 源动态 repostCount -1
        if (
            dyn.dynType is MomentTypeEnum.FORWARD
            and dyn.repostSrcDynId
            and before_normal
        ):
            await MomentStatService.incr_repost_count(session, dyn.repostSrcDynId, -1)

        session.add(
            _build_audit_log(
                moment_id=moment_id,
                operator_mid=operator_mid,
                to_status=MomentAuditStatusEnum.REJECTED,
                action=MomentAuditLogActionEnum.REJECT,
                from_status=from_status,
                reject_reason=reject_reason,
                remark=remark,
            )
        )
        await session.commit()
        await session.refresh(dyn)
        logger.info(f"管理员 {operator_mid} 审核驳回动态 dynId={moment_id}：{reject_reason}")
        # 弱依赖：通知作者（独立会话，失败不影响审核结果）
        await _notify_reject(operator_mid, dyn, reject_reason)
        briefs = await PptrUserService.get_many([dyn.mid])
        return _to_audit_item(dyn, briefs.get(dyn.mid))

    # ==================== 审核记录流水（P6-T4）====================

    @staticmethod
    async def log_list(
        session: AsyncSession,
        *,
        dyn_id: int | None = None,
        operator_mid: int | None = None,
        from_date: str | None = None,
        to_date: str | None = None,
        page_num: int = 1,
        page_size: int = 20,
    ) -> MomentAuditLogListResp:
        """审核记录流水查询（按 dynId / 操作员 / 时间段过滤）。"""
        page_num = max(1, page_num)
        page_size = min(max(1, page_size), 50)

        conditions = []
        if dyn_id is not None:
            conditions.append(col(TMomentAuditLog.dynId) == dyn_id)
        if operator_mid is not None:
            conditions.append(col(TMomentAuditLog.operatorMid) == operator_mid)
        if from_date:
            conditions.append(col(TMomentAuditLog.created_at) >= from_date)
        if to_date:
            conditions.append(col(TMomentAuditLog.created_at) <= to_date)

        total = int(
            (
                await session.exec(
                    select(func.count())
                    .select_from(TMomentAuditLog)
                    .where(*conditions)
                )
            ).one()
            or 0
        )
        rows = (
            await session.exec(
                select(TMomentAuditLog)
                .where(*conditions)
                .order_by(col(TMomentAuditLog.created_at).desc())
                .offset((page_num - 1) * page_size)
                .limit(page_size)
            )
        ).all()
        items = [_to_audit_log_item(r) for r in rows]
        return MomentAuditLogListResp(
            items=items, total=total, page_num=page_num, page_size=page_size
        )

    # ==================== 单条审核详情（P6-T5 GET /{dynId}）====================

    @staticmethod
    async def detail(
        session: AsyncSession, moment_id: int
    ) -> MomentAuditDetailResp:
        """单条动态审核详情：当前快照（含全部状态）+ 历史流转。"""
        dyn = await _get_any(session, moment_id)
        briefs = await PptrUserService.get_many([dyn.mid] if dyn else [])
        item = (
            _to_audit_item(dyn, briefs.get(dyn.mid)) if dyn is not None else None
        )
        logs = (
            await session.exec(
                select(TMomentAuditLog)
                .where(col(TMomentAuditLog.dynId) == moment_id)
                .order_by(col(TMomentAuditLog.created_at).desc())
            )
        ).all()
        return MomentAuditDetailResp(
            item=item, logs=[_to_audit_log_item(l) for l in logs]
        )


__all__ = ["MomentAuditService"]
