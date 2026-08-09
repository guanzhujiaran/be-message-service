"""私信管理端 HTTP 接口（/api/v1/message/dm/admin）。

Phase 5.x 私信审核能力：

- `POST /admin/audit`   人工审核：通过(pass) / 驳回(reject) / 下架(hidden) / 恢复(restore)，仅 root。
- `GET  /admin/audit`   审核队列；**root 可按任意状态查看全部内容**，
  被单独授权的管理员只能看到 `auditing`（待审核）。
- `GET  /admin/session` 会话上下文：按 msgkey / session_key 回捞整段会话（内容来源跳转落地）。
- `GET  /admin/stats`   私信全局统计（总数 / 今日新增 / 待审 / 驳回 / 下架）。

通过即把状态拨回 `normal` 对用户可见；驳回 / 下架置 `rejected` / `hidden`，
`DmService.list_messages` 已过滤该状态，聊天窗对用户不可见。
"""

from fastapi import APIRouter, Query

from app.core.database import SessionDep
from app.dependencies import MsgAdminUser, RootUser
from app.models import StandardResponse
from app.models.enums import DmAuditStateEnum
from app.models.schemas import (
    DmAuditItem,
    DmAuditListResp,
    DmAuditReq,
    DmBulkAuditReq,
    DmBulkAuditResp,
    DmSessionContextResp,
    DmStatsResp,
)
from app.services.dm_admin import DmAdminService

router = APIRouter(prefix="/api/v1/message/dm/admin", tags=["message-dm-admin"])


_OP_TO_STATE = {
    "pass": DmAuditStateEnum.NORMAL,
    "reject": DmAuditStateEnum.REJECTED,
    "hidden": DmAuditStateEnum.HIDDEN,
    "restore": DmAuditStateEnum.NORMAL,
}

# root 默认可见的状态：全部（可再用 state 参数收窄）
_ROOT_DEFAULT_STATES = list(DmAuditStateEnum)
# 非 root 管理员的硬上限：只能看待审核队列
_LIMITED_STATES = [DmAuditStateEnum.AUDITING]


@router.post("/audit", response_model=StandardResponse[DmAuditItem], summary="私信人工审核")
async def audit_dm(
    session: SessionDep,
    user: RootUser,
    req: DmAuditReq,
) -> StandardResponse[DmAuditItem]:
    """对一条私信执行审核操作（通过 / 驳回 / 下架 / 恢复）。

    仅 root 管理员可设置过审/没过审，其余管理员无此权限。
    """
    try:
        msgkey = int(str(req.msgkey).strip())
        if msgkey <= 0:
            raise ValueError("msgkey 不合法")
    except (TypeError, ValueError):
        return StandardResponse(code=400, msg="msgkey 不合法")

    state = _OP_TO_STATE.get(req.op)
    if state is None:
        return StandardResponse(code=400, msg="op 仅支持 pass/reject/hidden/restore")

    ok = await DmAdminService.set_state(session, msgkey, state)
    if not ok:
        return StandardResponse(code=404, msg="私信不存在")

    item = await DmAdminService.get_audit_item(session, msgkey)
    return StandardResponse(data=item)


@router.post(
    "/audit/batch",
    response_model=StandardResponse[DmBulkAuditResp],
    summary="批量私信人工审核",
)
async def bulk_audit_dm(
    session: SessionDep,
    user: RootUser,
    req: DmBulkAuditReq,
) -> StandardResponse[DmBulkAuditResp]:
    """对一批私信执行同一审核操作（通过 / 驳回 / 下架 / 恢复）。

    仅 root 管理员可操作。内部逐条复用单条审核逻辑（含通知投递）。
    """
    state = _OP_TO_STATE.get(req.op)
    if state is None:
        return StandardResponse(code=400, msg="op 仅支持 pass/reject/hidden/restore")

    msgkeys: list[int] = []
    for raw in req.msgkeys:
        try:
            v = int(str(raw).strip())
            if v > 0:
                msgkeys.append(v)
        except (TypeError, ValueError):
            continue

    if not msgkeys:
        return StandardResponse(code=400, msg="msgkeys 不能为空或格式非法")

    success, failed = await DmAdminService.bulk_set_state(
        session, msgkeys, state, note=req.note, notes=req.notes
    )
    return StandardResponse(
        data=DmBulkAuditResp(
            total=len(msgkeys),
            success=success,
            failed=[str(f) for f in failed],
        )
    )


@router.get("/audit", response_model=StandardResponse[DmAuditListResp], summary="私信审核队列")
async def audit_queue(
    session: SessionDep,
    user: MsgAdminUser,
    state: list[str] | None = Query(
        default=None,
        description="按状态过滤，如 normal / auditing / rejected / hidden，可多选；仅 root 可用",
    ),
    page_num: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=50),
) -> StandardResponse[DmAuditListResp]:
    """审核队列。

    可见范围按身份分层：

    - **root**：可查看全部状态的私信（默认全部，可用 `state` 收窄）；
    - **被单独授权的管理员**：只允许查看 `auditing`（待审核），
      显式请求其他状态直接 403；私信正文（内容明文）对审核队列开放可见。
    """
    if user.is_root:
        if state:
            try:
                states = [DmAuditStateEnum(s) for s in state]
            except ValueError:
                return StandardResponse(code=400, msg="state 取值非法")
        else:
            states = _ROOT_DEFAULT_STATES
    else:
        # 非 root：无论传什么，都只能落在待审核；显式越权请求直接拒绝
        if state and set(state) - {DmAuditStateEnum.AUDITING.value}:
            return StandardResponse(
                code=403, msg="无权限查看该状态的内容，仅可查看待审核私信"
            )
        states = _LIMITED_STATES

    items, total = await DmAdminService.list_audit_queue(
        session, states=states, page_num=page_num, page_size=page_size
    )
    # 私信正文（内容明文）对审核队列开放：非 root 仅能查看 auditing（待审核）内容，
    # 已审核内容只有 root 可见（由上方 states 限制保证）。
    return StandardResponse(
        data=DmAuditListResp(
            items=items,
            total=total,
            page_num=page_num,
            page_size=page_size,
            states=states,
            can_view_all_states=user.is_root,
        )
    )


@router.get(
    "/session",
    response_model=StandardResponse[DmSessionContextResp],
    summary="私信会话上下文（内容来源）",
)
async def session_context(
    session: SessionDep,
    user: MsgAdminUser,
    session_key: str | None = Query(default=None, description="会话键：小mid_大mid"),
    msgkey: str | None = Query(default=None, description="消息msgkey，用于反查会话"),
    page_size: int = Query(default=30, ge=1, le=100),
) -> StandardResponse[DmSessionContextResp]:
    """按 session_key（或 msgkey 反查）拉取该会话的消息上下文。

    审核队列里的一条私信是孤立的，没有上下文无法判断语境；管理端点击
    「内容来源」即调用本接口查看整段会话（内容明文对审核队列开放可见）。
    """
    key: int | None = None
    if msgkey:
        try:
            key = int(str(msgkey).strip())
            if key <= 0:
                raise ValueError("msgkey 不合法")
        except (TypeError, ValueError):
            return StandardResponse(code=400, msg="msgkey 不合法")
    if not session_key and key is None:
        return StandardResponse(code=400, msg="session_key 与 msgkey 至少提供一个")

    data = await DmAdminService.get_session_context(
        session, session_key=session_key, msgkey=key, page_size=page_size
    )
    if data is None:
        return StandardResponse(code=404, msg="会话不存在")

    return StandardResponse(data=data)


@router.get("/stats", response_model=StandardResponse[DmStatsResp], summary="私信统计")
async def admin_stats(
    session: SessionDep, user: MsgAdminUser
) -> StandardResponse[DmStatsResp]:
    """私信全局统计（管理端低频查询，管理员可见）。"""
    data = await DmAdminService.get_stats(session)
    return StandardResponse(data=data)


__all__ = ["router"]
