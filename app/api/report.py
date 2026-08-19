"""统一举报路由（2.14.0，动态 / 评论 / 用户空间三类举报合一）。

- `POST /api/v1/report`：统一举报入口（需登录），`biz_type`+`biz_id` 区分来源；
- `GET  /api/v1/report/admin/list`：管理端举报列表（root / 管理员）；
- `POST /api/v1/report/admin/review`：管理端举报审核（root / 管理员）。

统一异常处理：业务失败以 `ValueError` 抛出，路由层映射为 `400` 标准响应。
"""

from fastapi import APIRouter, Query

from app.core.database import SessionDep
from app.dependencies import AdminUser, RequiredUser
from app.models import StandardResponse
from app.models.schemas import (
    ReportCreateReq,
    ReportListResp,
    ReportReviewReq,
)
from app.services.report import ReportService

router = APIRouter(prefix="/api/v1/report", tags=["Report"])


@router.post("", response_model=StandardResponse, summary="统一举报（动态/评论/用户空间）")
async def create_report(
    session: SessionDep,
    user: RequiredUser,
    req: ReportCreateReq,
) -> StandardResponse:
    """统一举报：`biz_type`（dynamic/comment/user）+ `biz_id`（dynId/rpid/mid）区分来源。

    幂等：同一用户对同一对象只记一次；动态/评论达阈值自动转审核（分开计算）。
    """
    try:
        created, triggered = await ReportService.report(session, user.mid, req)
    except ValueError as e:
        return StandardResponse(code=400, msg=str(e))
    return StandardResponse(data={"created": created, "triggered": triggered})


@router.get(
    "/admin/list",
    response_model=StandardResponse[ReportListResp],
    summary="统一举报管理端列表",
)
async def list_reports(
    session: SessionDep,
    admin: AdminUser,
    biz_type: str | None = Query(default=None, description="按来源过滤：dynamic/comment/user"),
    status: str | None = Query(default=None, description="按状态过滤：pending/resolved/rejected"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=50),
) -> StandardResponse[ReportListResp]:
    """管理端统一举报列表（biz_type / status 过滤 + 分页，按创建时间倒序）。"""
    try:
        data = await ReportService.list_reports(
            session,
            biz_type=biz_type,
            status=status,
            page=page,
            page_size=page_size,
        )
    except ValueError as e:
        return StandardResponse(code=400, msg=str(e))
    return StandardResponse(data=data)


@router.post(
    "/admin/review",
    response_model=StandardResponse,
    summary="统一举报审核",
)
async def review(
    session: SessionDep,
    admin: AdminUser,
    req: ReportReviewReq,
) -> StandardResponse:
    """管理端审核举报：resolve（属实已处理）/ reject（驳回）。"""
    try:
        await ReportService.review(session, admin.mid, req)
    except ValueError as e:
        return StandardResponse(code=400, msg=str(e))
    return StandardResponse(data=None)


__all__ = ["router"]
