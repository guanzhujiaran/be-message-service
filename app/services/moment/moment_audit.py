"""动态审核服务（Phase 6；2.47.0 起审核**操作**对象化到 `interaction_actions`）。

本模块保留**查询类**静态方法：

- 管理员待审核列表（P6-T1）：auditStatus=auditing 按创建时间倒序分页。
- 审核记录流水查询（P6-T4）：按 dynId / 操作员 / 时间段过滤。

**审核通过 / 驳回已迁移**为 `interaction_actions/dynamic/audit.py` 的
`DynamicBiz.audit_approve` / `audit_reject`（DAC：`acl_scope=[AUDITOR_ONLY]`），
接口层直接实例化操作类执行，本模块辅助函数（`_get_any` / `_sync_resource_feed` /
`_build_audit_log` / `_notify_reject` / `_to_audit_item` / `_safe_author_brief`）继续复用。

作者信息（昵称 / 头像）经 ``PptrUser.get_many`` 只读回查（与评论 / Feed 一致，
不冗余用户快照）。审核操作均为管理员行为，operatorRole 记为 ``admin``。
"""

from datetime import datetime

from loguru import logger
from sqlmodel import col, func, select, update
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.db import TMoment, TResourceAuditLog, TResourceFeed
from bili_common.models import InteractionActionTypeEnum, InteractionBizTypeEnum
from app.models.enums import (
    MomentAuditLogActionEnum,
    MomentAuditLogOperatorRoleEnum,
    MomentTypeEnum,
    ResourceAuditStatusEnum,
)
from app.models.schemas.audit import AuditStatisticsResp
from app.services.moderation.audit_statistics import agg_rows_to_resp, status_key, type_key
from app.models.schemas.moment import (
    MomentAuditDetailResp,
    MomentAuditItem,
    MomentAuditListResp,
    MomentAuditLogItem,
    MomentAuditLogListResp,
    )
from app.services.moment.moment_stat import MomentStatService
from app.services.user.account import PptrUser


_AUDIT_STATUS_STR_MAP: dict[str, ResourceAuditStatusEnum] = {
    "normal": ResourceAuditStatusEnum.NORMAL,
    "auditing": ResourceAuditStatusEnum.AUDITING,
    "rejected": ResourceAuditStatusEnum.REJECTED,
    "hidden": ResourceAuditStatusEnum.HIDDEN,
    "deleted": ResourceAuditStatusEnum.DELETED,
}


def _normalize_audit_status(v: str | ResourceAuditStatusEnum) -> ResourceAuditStatusEnum:
    """兼容：Feed 审核态从字符串列改统一枚举后，历史调用方仍可能传字符串，做一次映射。"""
    if isinstance(v, ResourceAuditStatusEnum):
        return v
    try:
        return _AUDIT_STATUS_STR_MAP[v]
    except KeyError:
        raise ValueError(f"未知审核状态字符串: {v!r}") from None


async def _sync_resource_feed(
    session: AsyncSession,
    moment_id: int,
    *,
    audit_status: str | ResourceAuditStatusEnum | None = None,
    pub_time: datetime | None = None,
    deleted_at: datetime | None = None,
) -> None:
    """审核 / 删除时同步通用 Feed 元数据行（2.36.0）。

    仅更新传入字段；行不存在时静默跳过（发布链路已保证先建行）。
    `audit_status` 接受统一枚举或历史字符串（自动映射）。
    """
    values: dict[str, object] = {}
    if audit_status is not None:
        values["auditStatus"] = _normalize_audit_status(audit_status)
    if pub_time is not None:
        values["pubTime"] = pub_time
    if deleted_at is not None:
        values["deletedAt"] = deleted_at
    if not values:
        return
    await session.exec(
        update(TResourceFeed)
        .where(
            col(TResourceFeed.bizType) == InteractionBizTypeEnum.DYNAMIC,
            col(TResourceFeed.bizId) == moment_id,
        )
        .values(**values)
    )


def _iso(dt: datetime | None) -> str | None:
    """datetime → ISO 字符串（与现有服务一致，无 tz）。"""
    return dt.isoformat() if dt else None


async def _get_any(session: AsyncSession, moment_id: int) -> TMoment | None:
    """按 dynId 取动态（含全部状态，供审核后台查看；不按 normal / 软删过滤）。"""
    return (
        await session.exec(select(TMoment).where(col(TMoment.dynId) == moment_id))
    ).one_or_none()


def _to_audit_item(dyn: TMoment, author) -> MomentAuditItem:
    """TMoment → 审核队列卡片（author 来自 PptrUser.get_many 结果）。"""
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
        auditStatus=dyn.auditStatus.name,
        isTop=dyn.isTop,
        topicId=dyn.topicId,
    )


async def _safe_author_brief(mid: int):
    """弱依赖：取作者展示信息（昵称/头像），失败降级返回 None，不拖垮审核主流程。

    审核的状态变更 + 计数 + 审计流水已 ``commit``，作者信息仅用于响应展示；
    pptr Postgres 连接池在高并发（如 seed 灌数）下可能排队/超时，
    此时返回 None 让审核立即完成，避免接口"卡一下"甚至 500。
    """
    try:
        briefs = await PptrUser.get_many([mid])
        return briefs.get(mid)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"审核取作者信息失败（弱依赖，已降级）mid={mid}: {e}")
        return None


def _to_audit_log_item(log: TResourceAuditLog) -> MomentAuditLogItem:
    """TResourceAuditLog → 流水卡片（枚举转字符串；动态资源 `dynId` 透传 `bizId`，保持 API 契约稳定）。"""
    # 2.55.0（2026-09-02）：通用资源审核流水 `TMomentAuditLog → TResourceAuditLog` 改造；
    # API 响应仍出 `dynId/operatorMid` 旧名（值分别取自 base 的 `bizId`/`mid`），保持前端契约不变。
    return MomentAuditLogItem(
        pk=log.pk,
        dynId=log.bizId,
        operatorMid=log.mid,
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
    biz_type: InteractionBizTypeEnum,
    biz_id: int,
    operator_mid: int,
    to_status: ResourceAuditStatusEnum,
    action: MomentAuditLogActionEnum,
    from_status: ResourceAuditStatusEnum | None = None,
    reject_reason: str | None = None,
    remark: str | None = None,
) -> TResourceAuditLog:
    """构造一条通用资源审核流转记录（`ResourceBase` 子表，2.55.0 起）。"""
    return TResourceAuditLog(
        bizType=biz_type,
        bizId=biz_id,
        mid=operator_mid,
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
    from app.services.message.insite.events import report_event_weakly

    await report_event_weakly(
        _EventReportReq_for_reject(operator_mid, dyn, reject_reason),
    )


def _EventReportReq_for_reject(operator_mid: int, dyn: TMoment, reject_reason: str):
    from app.models.schemas import EventReportReq

    return EventReportReq(
        mid=dyn.mid,
        event_type=InteractionActionTypeEnum.AUDIT_REJECT,
        source_type=InteractionBizTypeEnum.DYNAMIC,
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
        audit_status: ResourceAuditStatusEnum = ResourceAuditStatusEnum.AUDITING,
        biz_type: MomentTypeEnum | None = None,
        page_num: int = 1,
        page_size: int = 20,
    ) -> MomentAuditListResp:
        """管理员审核列表：按 audit_status 过滤（默认 auditing，保持旧行为），按创建时间倒序，分页。

        2.30.0 起支持按状态筛选：auditing（待审核）/ normal（已过审，可执行「驳回」撤回）/
        rejected（已驳回，可执行「通过」恢复）/ hidden（已下架）。
        `biz_type`（动态子类型 WORD/FORWARD/…）与各 admin list 接口统一：
        有资源子类型维度的域均用该参数筛选（举报 / 评论同此风格）。
        """
        page_num = max(1, page_num)
        page_size = min(max(1, page_size), 50)

        conditions = [col(TMoment.auditStatus) == audit_status]
        if biz_type is not None:
            conditions.append(col(TMoment.dynType) == biz_type)

        total = int(
            (
                await session.exec(
                    select(func.count()).select_from(TMoment).where(*conditions)
                )
            ).one()
            or 0
        )
        rows = (
            await session.exec(
                select(TMoment)
                .where(*conditions)
                .order_by(col(TMoment.created_at).desc())
                .offset((page_num - 1) * page_size)
                .limit(page_size)
            )
        ).all()

        mids = {r.mid for r in rows}
        briefs = await PptrUser.get_many(list(mids))
        items = [_to_audit_item(r, briefs.get(r.mid)) for r in rows]
        return MomentAuditListResp(
            items=items, total=total, page_num=page_num, page_size=page_size
        )

    # ==================== 审核总统计（Phase M）====================

    @staticmethod
    async def statistics(session: AsyncSession) -> AuditStatisticsResp:
        """动态审核总统计：按 dynType × auditStatus 二维聚合 TMoment。

        一次 GROUP BY 完成，禁止循环发 COUNT；返回类型化统计供管理后台概览。
        """
        rows = (
            await session.exec(
                select(TMoment.dynType, TMoment.auditStatus, func.count())
                .select_from(TMoment)
                .group_by(TMoment.dynType, TMoment.auditStatus)
            )
        ).all()

        return agg_rows_to_resp(
            [
                (
                    type_key(dyn_type),
                    status_key(audit_status),
                    cnt,
                )
                for dyn_type, audit_status, cnt in rows
            ]
        )

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
        """审核记录流水查询（按 dynId / 操作员 / 时间段过滤，2.55.0 起落 `TResourceAuditLog`）。

        API 入参 `dyn_id` 语义不变（= 动态资源时 bizId），内部按
        `(bizType=DYNAMIC, bizId=dyn_id)` 过滤；`operator_mid` 对应 base 的 `mid`。
        """
        page_num = max(1, page_num)
        page_size = min(max(1, page_size), 50)

        conditions = []
        if dyn_id is not None:
            conditions.append(col(TResourceAuditLog.bizType) == InteractionBizTypeEnum.DYNAMIC)
            conditions.append(col(TResourceAuditLog.bizId) == dyn_id)
        if operator_mid is not None:
            conditions.append(col(TResourceAuditLog.mid) == operator_mid)
        if from_date:
            conditions.append(col(TResourceAuditLog.created_at) >= from_date)
        if to_date:
            conditions.append(col(TResourceAuditLog.created_at) <= to_date)

        total = int(
            (
                await session.exec(
                    select(func.count())
                    .select_from(TResourceAuditLog)
                    .where(*conditions)
                )
            ).one()
            or 0
        )
        rows = (
            await session.exec(
                select(TResourceAuditLog)
                .where(*conditions)
                .order_by(col(TResourceAuditLog.created_at).desc())
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
        """单条动态审核详情：当前快照（含全部状态）+ 历史流转。

        2.55.0 起历史流水按 `(bizType=DYNAMIC, bizId=moment_id)` 过滤。
        """
        dyn = await _get_any(session, moment_id)
        briefs = await PptrUser.get_many([dyn.mid] if dyn else [])
        item = (
            _to_audit_item(dyn, briefs.get(dyn.mid)) if dyn is not None else None
        )
        logs = (
            await session.exec(
                select(TResourceAuditLog)
                .where(
                    col(TResourceAuditLog.bizType) == InteractionBizTypeEnum.DYNAMIC,
                    col(TResourceAuditLog.bizId) == moment_id,
                )
                .order_by(col(TResourceAuditLog.created_at).desc())
            )
        ).all()
        return MomentAuditDetailResp(
            item=item, logs=[_to_audit_log_item(l) for l in logs]
        )


__all__ = ["MomentAuditService"]
