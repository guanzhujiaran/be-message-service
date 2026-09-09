"""通用资源审核 HTTP 接口（/api/v1/audit，计划书 §5.13）。

与「各资源专用审核路由」（动态 dynId / 话题 topicId / 头像·封面 pk / 评论 rpid）
并存，但**前端统一走本接口**：只按 `bizType` + `bizId` 定位资源，路由层不做任何
bizType 分流，动作一律下沉到资源类方法（驳回通知也在资源方法内，见
`BaseBiz._notify_audit_result` 与各资源 `audit_approve/audit_reject`）：

- ``POST /approve``  审核通过
- ``POST /reject``   审核驳回（必须通知作者）

权限：仅 ``role=root``（与其它审核路由同守卫）。
"""

from fastapi import APIRouter

from app.core.database import SessionDep
from app.dependencies import RootUser
from app.models import StandardResponse
from app.models.schemas.audit import (
    AuditActionResp,
    AuditApproveReq,
    AuditRejectReq,
)
from app.services.interaction_actions import get_biz

router = APIRouter(prefix="/api/v1/audit", tags=["audit"])


def _resp(biz_type, biz_id: int, data) -> StandardResponse[AuditActionResp]:
    return StandardResponse(
        data=AuditActionResp(
            bizType=biz_type,
            bizId=biz_id,
            bizIdStr=str(biz_id),
            data=data if isinstance(data, dict) else None,
        )
    )


@router.post(
    "/approve",
    response_model=StandardResponse[AuditActionResp],
    summary="通用审核通过（bizType + bizId）",
)
async def audit_approve(
    session: SessionDep,
    user: RootUser,
    req: AuditApproveReq,
) -> StandardResponse[AuditActionResp]:
    try:
        biz = get_biz(req.bizType, session, int(req.bizId), user.mid)
        data = await biz.audit_approve(remark=req.remark)
    except ValueError as e:
        return StandardResponse(code=404, msg=str(e))
    except NotImplementedError as e:
        return StandardResponse(code=400, msg=str(e))
    return _resp(req.bizType, int(req.bizId), data)


@router.post(
    "/reject",
    response_model=StandardResponse[AuditActionResp],
    summary="通用审核驳回（bizType + bizId）",
)
async def audit_reject(
    session: SessionDep,
    user: RootUser,
    req: AuditRejectReq,
) -> StandardResponse[AuditActionResp]:
    try:
        biz = get_biz(req.bizType, session, int(req.bizId), user.mid)
        data = await biz.audit_reject(
            reject_reason=req.rejectReason, remark=req.remark
        )
    except ValueError as e:
        return StandardResponse(code=404, msg=str(e))
    except NotImplementedError as e:
        return StandardResponse(code=400, msg=str(e))
    return _resp(req.bizType, int(req.bizId), data)


__all__ = ["router"]
