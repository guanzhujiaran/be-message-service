"""RPA 资源标签（rpa_tag）用户侧路由（be-message 全托管，2.49.0）。

创建 / 打标（attach / detach）/ 列表（list / list-by-target）在下放后统一走
be-message，经 RPC 回调 RPA 存储（`rpa_rpc_client.create_tag / attach_tag /
detach_tag / list_tags`）。审核（通过/驳回）复用通用
`/api/v1/audit/approve|reject`（bizType=RPA_TAG，见 `audit.py`）。

权限：
- 创建 / 打标 / detach：任意登录用户（`RequiredUser`）。
- 列表：普通用户仅 `normal`；`role=root` 可传 `auditing` / `rejected` / `all`
  供管理审核页筛选。
"""

from fastapi import APIRouter

from app.dependencies import RequiredUser
from app.models import StandardResponse
from app.models.schemas.rpa_tag import (
    RpaTagAttachReq,
    RpaTagCreateReq,
    RpaTagDetachReq,
    RpaTagItemResp,
    RpaTagListByTargetReq,
    RpaTagListReq,
    RpaTagListResp,
)
from app.services.infrastructure.rpa_rpc import rpa_rpc_client

router = APIRouter(prefix="/api/v1/rpa_tag", tags=["rpa_tag"])


def _to_item(tag) -> RpaTagItemResp:
    return RpaTagItemResp(
        id=tag.id,
        name=tag.name,
        color=tag.color,
        createdBy=getattr(tag, "createdBy", None),
        auditStatus=tag.auditStatus,
        pubTime=tag.pubTime.isoformat() if getattr(tag, "pubTime", None) else None,
        createdAt=tag.createdAt.isoformat() if getattr(tag, "createdAt", None) else None,
    )


@router.post(
    "/create", response_model=StandardResponse[RpaTagItemResp], summary="创建资源标签（进入待审核）"
)
async def create_tag(
    user: RequiredUser,
    req: RpaTagCreateReq,
) -> StandardResponse[RpaTagItemResp]:
    name = (req.name or "").strip()
    if not name:
        return StandardResponse(code=400, msg="标签名不能为空")
    result = await rpa_rpc_client.create_tag(
        name=name, color=req.color, created_mid=user.mid
    )
    if result is None:
        return StandardResponse(code=500, msg="标签服务暂不可用，请稍后重试")
    if not result.success:
        return StandardResponse(code=400, msg=result.message or "创建失败")
    return StandardResponse(
        data=RpaTagItemResp(
            id=int(result.id or 0),
            name=name,
            color=req.color,
            createdBy=user.mid,
            auditStatus="auditing",
            pubTime=None,
            createdAt=None,
        ),
        msg="标签已提交，待审核",
    )


@router.post(
    "/attach", response_model=StandardResponse, summary="为资源关联标签（仅可关联已审核通过标签）"
)
async def attach_tag(
    user: RequiredUser,
    req: RpaTagAttachReq,
) -> StandardResponse:
    result = await rpa_rpc_client.attach_tag(
        tag_id=req.tagId,
        target_type=req.targetType,
        target_id=req.targetId,
        created_mid=user.mid,
    )
    if result is None:
        return StandardResponse(code=500, msg="标签服务暂不可用，请稍后重试")
    if not result.success:
        return StandardResponse(code=400, msg=result.message or "关联失败")
    return StandardResponse(msg="已关联标签")


@router.post(
    "/detach", response_model=StandardResponse, summary="移除资源上的标签"
)
async def detach_tag(
    user: RequiredUser,
    req: RpaTagDetachReq,
) -> StandardResponse:
    result = await rpa_rpc_client.detach_tag(
        tag_id=req.tagId, target_type=req.targetType, target_id=req.targetId
    )
    if result is None:
        return StandardResponse(code=500, msg="标签服务暂不可用，请稍后重试")
    if not result.success:
        return StandardResponse(code=400, msg=result.message or "移除失败")
    return StandardResponse(msg="已移除标签")


@router.post(
    "/list", response_model=StandardResponse[RpaTagListResp], summary="列出标签（普通用户仅 normal，root 可按状态筛选）"
)
async def list_tags(
    user: RequiredUser,
    req: RpaTagListReq,
) -> StandardResponse[RpaTagListResp]:
    # 普通用户强制仅 normal；root 可传 auditing/rejected/all 用于审核页
    is_root = user.role == "root"
    audit_status = req.auditStatus if is_root else None
    result = await rpa_rpc_client.list_tags(
        audit_status=audit_status, page=req.page, per_page=req.perPage
    )
    if result is None:
        return StandardResponse(code=500, msg="标签服务暂不可用，请稍后重试")
    return StandardResponse(
        data=RpaTagListResp(
            page=req.page,
            perPage=req.perPage,
            total=result.total,
            items=[_to_item(t) for t in result.items],
        )
    )


@router.post(
    "/list-by-target",
    response_model=StandardResponse[list[RpaTagItemResp]],
    summary="查询某资源关联的标签（仅返回已审核通过）",
)
async def list_tags_by_target(
    user: RequiredUser,
    req: RpaTagListByTargetReq,
) -> StandardResponse[list[RpaTagItemResp]]:
    result = await rpa_rpc_client.list_tags_by_target(
        target_type=req.targetType, target_id=req.targetId
    )
    if result is None:
        return StandardResponse(code=500, msg="标签服务暂不可用，请稍后重试")
    return StandardResponse(data=[_to_item(t) for t in result.items])


__all__ = ["router"]