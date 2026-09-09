"""话题创建审核管理接口（2.19.0，root 守卫，对齐动态审核 P6）。

- ``GET  /list``     管理员话题待审核列表（auditing 队列分页）
- ``POST /approve``  审核通过：auditStatus→normal + pubTime=now()，**不发通知**
- ``POST /reject``   审核驳回：auditStatus→rejected + 写原因，**发驳回通知给创建者**
"""

from fastapi import APIRouter, Query

from app.core.database import SessionDep
from app.dependencies import RootUser
from app.models import StandardResponse
from app.models.enums import ResourceAuditStatusEnum
from app.models.schemas.moment import (
    MomentTopicAuditApproveReq,
    MomentTopicAuditListResp,
    MomentTopicAuditRejectReq,
)
from app.services.moment.moment_topic_audit import MomentTopicAuditService

router = APIRouter(prefix="/api/v1/community/topic/audit", tags=["moment-topic-audit"])


@router.get(
    "/list",
    response_model=StandardResponse[MomentTopicAuditListResp],
    summary="管理员话题待审核列表",
)
async def topic_audit_list(
    session: SessionDep,
    user: RootUser,
    auditStatus: ResourceAuditStatusEnum = Query(
        default=ResourceAuditStatusEnum.AUDITING,
        description="审核状态筛选：auditing（默认）/normal/rejected/hidden",
    ),
    page_num: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=50),
) -> StandardResponse[MomentTopicAuditListResp]:
    data = await MomentTopicAuditService.pending_list(
        session, audit_status=auditStatus, page_num=page_num, page_size=page_size
    )
    return StandardResponse(data=data)


@router.post(
    "/approve",
    response_model=StandardResponse[MomentTopicAuditListResp],
    summary="审核通过话题",
)
async def topic_audit_approve(
    session: SessionDep,
    user: RootUser,
    req: MomentTopicAuditApproveReq,
) -> StandardResponse[MomentTopicAuditListResp]:
    try:
        await MomentTopicAuditService.approve(
            session, req.topicId, operator_mid=user.mid, remark=req.remark
        )
    except ValueError as e:
        return StandardResponse(code=404, msg=str(e))
    return StandardResponse(msg="审核通过")


@router.post(
    "/reject",
    response_model=StandardResponse[MomentTopicAuditListResp],
    summary="审核驳回话题",
)
async def topic_audit_reject(
    session: SessionDep,
    user: RootUser,
    req: MomentTopicAuditRejectReq,
) -> StandardResponse[MomentTopicAuditListResp]:
    try:
        await MomentTopicAuditService.reject(
            session,
            req.topicId,
            operator_mid=user.mid,
            reject_reason=req.rejectReason,
            remark=req.remark,
        )
    except ValueError as e:
        return StandardResponse(code=404, msg=str(e))
    return StandardResponse(msg="审核驳回")


__all__ = ["router"]
