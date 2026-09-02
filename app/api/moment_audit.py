"""动态审核管理端 HTTP 接口（/api/v1/community/audit）。

Phase 6 的审核能力（P6-T1 / P6-T3 / P6-T4 / P6-T5）：

- ``GET  /list``         管理员待审核列表（auditStatus=auditing，按创建时间倒序分页）
- ``GET  /list/history``  审核记录流水（按 dynId / 操作员 / 时间段过滤）
- ``POST /approve``       审核通过（→ normal + pubTime，写 AuditLog，FORWARD 时源动态 repostCount +1；不发通知）
- ``POST /reject``        审核驳回（→ rejected + 驳回原因，写 AuditLog，发驳回事件通知；FORWARD ∧ before=normal 时源动态 -1）
- ``GET  /{dynId}``       单条动态审核详情（含全部状态 + 历史流转）

全部接口仅 ``role=root`` 可访问（P6-T5 权限守卫）。
"""

from fastapi import APIRouter, Query

from app.core.database import SessionDep
from app.dependencies import RootUser
from app.models import StandardResponse
from bili_common.models import InteractionBizTypeEnum
from app.models.enums import MomentAuditStatusEnum
from app.models.str_int import StrInt
from app.models.schemas.moment import (
    MomentAuditActionReq,
    MomentAuditDetailResp,
    MomentAuditListResp,
    MomentAuditLogListResp,
    MomentAuditRejectReq,
    MomentAuditStatisticsResp,
)
from app.services.interaction_actions import get_biz
from app.services.moment.moment_audit import MomentAuditService

router = APIRouter(prefix="/api/v1/community/audit", tags=["moment-audit"])


@router.get(
    "/list",
    response_model=StandardResponse[MomentAuditListResp],
    summary="管理员审核列表（可按状态筛选）",
)
async def audit_list(
    session: SessionDep,
    user: RootUser,
    auditStatus: MomentAuditStatusEnum = Query(
        default=MomentAuditStatusEnum.AUDITING,
        description="审核状态筛选：auditing（默认，待审核）/normal（已过审）/rejected（已驳回）/hidden（已下架）",
    ),
    page_num: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=50),
) -> StandardResponse[MomentAuditListResp]:
    data = await MomentAuditService.pending_list(
        session,
        audit_status=auditStatus,
        page_num=page_num,
        page_size=page_size,
    )
    return StandardResponse(data=data)


@router.get(
    "/statistics",
    response_model=StandardResponse[MomentAuditStatisticsResp],
    summary="动态审核总统计（按类型 + 按状态）",
)
async def audit_statistics(
    session: SessionDep,
    user: RootUser,
) -> StandardResponse[MomentAuditStatisticsResp]:
    data = await MomentAuditService.statistics(session)
    return StandardResponse(data=MomentAuditStatisticsResp(**data))


@router.get(
    "/list/history",
    response_model=StandardResponse[MomentAuditLogListResp],
    summary="审核记录流水",
)
async def audit_history(
    session: SessionDep,
    user: RootUser,
    dynId: StrInt | None = Query(default=None, description="按动态 ID 过滤（雪花 ID，StrInt 兼容前端 str 传参）"),
    operatorMid: StrInt | None = Query(default=None, description="按操作员 MID 过滤（雪花 ID，StrInt 兼容前端 str 传参）"),
    fromDate: str | None = Query(default=None, description="起始时间 YYYY-MM-DD HH:MM:SS"),
    toDate: str | None = Query(default=None, description="结束时间 YYYY-MM-DD HH:MM:SS"),
    page_num: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=50),
) -> StandardResponse[MomentAuditLogListResp]:
    data = await MomentAuditService.log_list(
        session,
        dyn_id=dynId,
        operator_mid=operatorMid,
        from_date=fromDate,
        to_date=toDate,
        page_num=page_num,
        page_size=page_size,
    )
    return StandardResponse(data=data)


@router.post(
    "/approve",
    response_model=StandardResponse[MomentAuditDetailResp],
    summary="审核通过",
)
async def audit_approve(
    session: SessionDep,
    user: RootUser,
    req: MomentAuditActionReq,
) -> StandardResponse[MomentAuditDetailResp]:
    try:
        biz = get_biz(InteractionBizTypeEnum.DYNAMIC, session, req.dynId, user.mid)
        item = await biz.audit_approve(remark=req.remark)
    except ValueError as e:
        return StandardResponse(code=404, msg=str(e))
    return StandardResponse(
        data=MomentAuditDetailResp(item=item, logs=[])
    )


@router.post(
    "/reject",
    response_model=StandardResponse[MomentAuditDetailResp],
    summary="审核驳回",
)
async def audit_reject(
    session: SessionDep,
    user: RootUser,
    req: MomentAuditRejectReq,
) -> StandardResponse[MomentAuditDetailResp]:
    try:
        biz = get_biz(InteractionBizTypeEnum.DYNAMIC, session, req.dynId, user.mid)
        item = await biz.audit_reject(reject_reason=req.rejectReason, remark=req.remark)
    except ValueError as e:
        return StandardResponse(code=404, msg=str(e))
    return StandardResponse(
        data=MomentAuditDetailResp(item=item, logs=[])
    )


@router.get(
    "/{dynId}",
    response_model=StandardResponse[MomentAuditDetailResp],
    summary="单条动态审核详情",
)
async def audit_detail(
    session: SessionDep,
    user: RootUser,
    dynId: StrInt,
) -> StandardResponse[MomentAuditDetailResp]:
    data = await MomentAuditService.detail(session, dynId)
    if data.item is None:
        return StandardResponse(code=404, msg="动态不存在")
    return StandardResponse(data=data)


__all__ = ["router"]
