"""评论管理端 HTTP 接口（/api/v1/comment/admin，reply-admin）。

Phase 5.3 的管理能力：

- `POST /admin/audit`     人工审核：通过(pass) / 驳回(reject) / 下架(hidden) / 恢复(restore)，仅 root。
- `GET  /admin/audit`     审核队列；**root 可按任意状态查看全部内容**，
  被单独授权的管理员只能看到 `auditing`（待审核）。
- `GET  /admin/source/{rpid}` 内容来源：评论所属评论区 + 前端可直接跳转的地址。
- `GET  /admin/ip/{rpid}` 查看原始 IP（D3：仅管理端可见明文，root 专属）。
- `GET  /admin/stats`     评论区全局统计（总数 / 今日新增 / 用户排行）。

审核通过即把状态拨回 `normal` 对外可见；驳回 / 下架置 `rejected` / `hidden`；
恢复则把被下架 / 驳回的评论重新置 `normal`。
"""

from fastapi import APIRouter, Query

from app.core.database import SessionDep
from app.dependencies import MsgAdminUser, RootUser
from app.models import StandardResponse
from app.models.enums import CommentStateEnum
from app.models.schemas import (
    CommentAuditItem,
    CommentAuditListResp,
    CommentAuditReq,
    CommentBulkAuditReq,
    CommentBulkAuditResp,
    CommentSourceResp,
    CommentStatsResp,
)
from app.services.comment_admin import CommentAdminService

router = APIRouter(prefix="/api/v1/comment/admin", tags=["comment-admin"])


_OP_TO_STATE = {
    "pass": CommentStateEnum.NORMAL,
    "reject": CommentStateEnum.REJECTED,
    "hidden": CommentStateEnum.HIDDEN,
    "restore": CommentStateEnum.NORMAL,
}

# root 默认可见的状态：全部（可再用 state 参数收窄）
_ROOT_DEFAULT_STATES = list(CommentStateEnum)
# 非 root 管理员的硬上限：只能看待审核队列
_LIMITED_STATES = [CommentStateEnum.AUDITING]


@router.post("/audit", response_model=StandardResponse[CommentAuditItem], summary="人工审核")
async def audit_comment(
    session: SessionDep,
    user: RootUser,
    req: CommentAuditReq,
) -> StandardResponse[CommentAuditItem]:
    """对一条评论执行审核操作（通过 / 驳回 / 下架 / 恢复）。

    仅 root 管理员可设置过审/没过审（含将已 normal 的评论重新审核），其余管理员无此权限。
    """
    try:
        rpid = int(str(req.rpid).strip())
        if rpid <= 0:
            raise ValueError("rpid 不合法")
    except (TypeError, ValueError):
        return StandardResponse(code=400, msg="rpid 不合法")

    state = _OP_TO_STATE.get(req.op)
    if state is None:
        return StandardResponse(code=400, msg="op 仅支持 pass/reject/hidden/restore")

    ok = await CommentAdminService.set_state(
        session, rpid, state, note=req.note, operator_mid=user.mid
    )
    if not ok:
        return StandardResponse(code=404, msg="评论不存在")

    # 审核后直接回查该条（含明文 IP），即使状态非 normal 也能展示
    item = await CommentAdminService.get_audit_item(session, rpid)
    return StandardResponse(data=item)


@router.post(
    "/audit/batch",
    response_model=StandardResponse[CommentBulkAuditResp],
    summary="批量人工审核",
)
async def bulk_audit_comment(
    session: SessionDep,
    user: RootUser,
    req: CommentBulkAuditReq,
) -> StandardResponse[CommentBulkAuditResp]:
    """对一批评论执行同一审核操作（通过 / 驳回 / 下架 / 恢复）。

    仅 root 管理员可操作。内部逐条复用单条审核逻辑（含通知投递）。
    """
    state = _OP_TO_STATE.get(req.op)
    if state is None:
        return StandardResponse(code=400, msg="op 仅支持 pass/reject/hidden/restore")

    rpids: list[int] = []
    for raw in req.rpids:
        try:
            v = int(str(raw).strip())
            if v > 0:
                rpids.append(v)
        except (TypeError, ValueError):
            continue

    if not rpids:
        return StandardResponse(code=400, msg="rpids 不能为空或格式非法")

    success, failed = await CommentAdminService.bulk_set_state(
        session, rpids, state, note=req.note, notes=req.notes, operator_mid=user.mid
    )
    return StandardResponse(
        data=CommentBulkAuditResp(
            total=len(rpids),
            success=success,
            failed=[str(f) for f in failed],
        )
    )


@router.get("/audit", response_model=StandardResponse[CommentAuditListResp], summary="审核队列")
async def audit_queue(
    session: SessionDep,
    user: MsgAdminUser,
    state: list[str] | None = Query(
        default=None,
        description="按状态过滤，如 normal / auditing / rejected / hidden / deleted，可多选；仅 root 可用",
    ),
    page_num: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=50),
) -> StandardResponse[CommentAuditListResp]:
    """审核队列。

    可见范围按身份分层：

    - **root**：可查看全部状态的评论（默认全部，可用 `state` 收窄）；
    - **被单独授权的管理员**：只允许查看 `auditing`（待审核），
      显式请求其他状态直接 403；评论正文（内容明文）对审核队列开放可见，
      但明文 IP 仍仅 root 可见。
    """
    if user.is_root:
        if state:
            try:
                states = [CommentStateEnum(s) for s in state]
            except ValueError:
                return StandardResponse(code=400, msg="state 取值非法")
        else:
            states = _ROOT_DEFAULT_STATES
    else:
        # 非 root：无论传什么，都只能落在待审核；显式越权请求直接拒绝
        if state and set(state) - {CommentStateEnum.AUDITING.value}:
            return StandardResponse(
                code=403, msg="无权限查看该状态的内容，仅可查看待审核评论"
            )
        states = _LIMITED_STATES

    items, total = await CommentAdminService.list_audit_queue(
        session, states=states, page_num=page_num, page_size=page_size
    )
    # 评论正文（内容明文）对审核队列开放：非 root 仅能查看 auditing（待审核）内容，
    # 已审核内容（normal/rejected/hidden）只有 root 可见（由上方 states 限制保证）。
    # 明文 IP 仍仅 root 可见，非 root 直接抹除。
    if not user.is_root:
        for it in items:
            it.ip_v4 = None
            it.ip_v6 = None
    return StandardResponse(
        data=CommentAuditListResp(
            items=items,
            total=total,
            page_num=page_num,
            page_size=page_size,
            states=states,
            can_view_all_states=user.is_root,
        )
    )


@router.get(
    "/source/{rpid}",
    response_model=StandardResponse[CommentSourceResp],
    summary="评论内容来源",
)
async def comment_source(
    session: SessionDep,
    user: MsgAdminUser,
    rpid: str,
) -> StandardResponse[CommentSourceResp]:
    """按 rpid 返回该评论的内容来源（评论区归属、UP 主、可跳转地址）。

    管理端「内容来源」链接点击时按需拉取；不含评论正文，任意管理员可见。
    """
    try:
        rpid_int = int(str(rpid).strip())
        if rpid_int <= 0:
            raise ValueError("rpid 不合法")
    except (TypeError, ValueError):
        return StandardResponse(code=400, msg="rpid 不合法")

    data = await CommentAdminService.get_source(session, rpid_int)
    if data is None:
        return StandardResponse(code=404, msg="评论不存在")
    return StandardResponse(data=data)


@router.get("/ip/{rpid}", summary="查看原始IP（管理端）")
async def admin_ip(
    session: SessionDep,
    user: RootUser,
    rpid: str,
) -> StandardResponse[dict]:
    """查看一条评论的原始 IPv4 / IPv6（D3：仅管理端可见明文，root 专属）。"""
    try:
        rpid_int = int(str(rpid).strip())
        if rpid_int <= 0:
            raise ValueError("rpid 不合法")
    except (TypeError, ValueError):
        return StandardResponse(code=400, msg="rpid 不合法")

    ip_v4, ip_v6 = await CommentAdminService.get_plaintext_ip(session, rpid_int)
    return StandardResponse(data={"rpid": str(rpid_int), "ip_v4": ip_v4, "ip_v6": ip_v6})


@router.get("/stats", response_model=StandardResponse[CommentStatsResp], summary="评论统计")
async def admin_stats(
    session: SessionDep, user: MsgAdminUser
) -> StandardResponse[CommentStatsResp]:
    """评论区全局统计（管理端低频查询，管理员可见）。"""
    data = await CommentAdminService.get_stats(session)
    return StandardResponse(data=data)


__all__ = ["router"]
