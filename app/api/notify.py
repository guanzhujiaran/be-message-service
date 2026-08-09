"""系统通知模块 HTTP 接口（/api/v1/message/notify）。

面向两类调用方：

- **管理员**（role=root）：发布 / 修改 / 撤回通知，可按用户类型（全体 / 角色 /
  等级 / 大会员 / 指定 mid）投放，支持定时发布与过期时间。
- **普通用户**：定时拉取增量（游标语义，天然避免重复消费）、分页查看历史、
  标记已读。
- **系统通知不允许普通用户删除**：每条系统通知（如审核驳回告知）对所有用户
  一致可见，用户侧只能标记已读，删除只能由管理员在管理界面（撤回）进行。
  因此 `/notify/delete` 已收敛为仅管理员可用，普通用户调用会被拒绝。

认证同项目其它微服务：完全依赖上游 nodejs-pptr 注入的 x-bili-* 请求头，
不做令牌校验（网关侧已重写为可信登录态）。
"""

from fastapi import APIRouter, Query

from app.core.database import SessionDep
from app.dependencies import AdminUser, RequiredUser
from app.models import StandardResponse
from app.models.enums import NotifyStatusEnum
from app.models.schemas import (
    BiliSystemNotifyResp,
    NotifyAdminItem,
    NotifyAdminListResp,
    NotifyCreateReq,
    NotifyListResp,
    NotifyPullResp,
    NotifyReadReq,
    NotifyReadResp,
    NotifyUpdateReq,
    SystemNotifyItem,
    SystemNotifyListResp,
)
from app.services.activity import ActivityService
from app.services.notify import NotifyService

router = APIRouter(prefix="/api/v1/message/notify", tags=["message-notify"])


# ==================== 用户侧 ====================


@router.get("/pull", response_model=StandardResponse[NotifyPullResp], summary="定时拉取增量通知")
async def pull_notify(
    session: SessionDep,
    user: RequiredUser,
    cursor: int | None = Query(default=None, description="客户端游标，不传则用服务端持久化游标"),
    limit: int = Query(default=20, ge=1, le=100),
) -> StandardResponse[NotifyPullResp]:
    """拉取本用户可见的增量通知。

    只返回 `id > cursor` 的通知，拉取后服务端会推进游标，
    因此**重复调用不会拿到重复数据**（即使客户端丢了本地游标）。
    每次拉取同时记一次用户活跃，用于后续推送策略分流。
    """
    await ActivityService.touch(session, user.mid)
    data = await NotifyService.pull(session, user, cursor=cursor, limit=limit)
    return StandardResponse(data=data)


@router.get(
    "/list", response_model=StandardResponse[NotifyListResp], summary="分页查看历史通知"
)
async def list_notify(
    session: SessionDep,
    user: RequiredUser,
    page_num: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    only_unread: bool = Query(default=False, description="仅看未读"),
) -> StandardResponse[NotifyListResp]:
    """分页查看历史通知（不推进拉取游标）。"""
    items, total = await NotifyService.list_for_user(
        session, user, page_num=page_num, page_size=page_size, only_unread=only_unread
    )
    return StandardResponse(
        data=NotifyListResp(
            items=items, total=total, page_num=page_num, page_size=page_size
        )
    )


@router.get("/unread", response_model=StandardResponse[int], summary="系统通知未读数")
async def unread_notify(session: SessionDep, user: RequiredUser) -> StandardResponse[int]:
    return StandardResponse(data=await NotifyService.unread_count(session, user))


@router.get(
    "/system",
    response_model=BiliSystemNotifyResp,
    summary="系统通知列表（模仿 B 站 feedsystem/system_notify/get）",
)
async def system_notify_bili(
    session: SessionDep,
    user: RequiredUser,
    page_num: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
) -> BiliSystemNotifyResp:
    """模仿 B 站 `/x/v2/feedsystem/system_notify/get` 接口。

    返回结构与 B 站保持一致：`code/msg/message/ttl` 外壳 + `data.system_notify_list`。
    列表项字段（`cursor` 纳秒时间戳、`content` 为 `{"web": "..."}` 的 JSON 字符串、
    `type` 固定为 4、`is_send` 映射 `dispatched` 等）均与 B 站对齐。
    评论审核驳回等系统通知会以同样形态出现在该列表中。
    """
    await ActivityService.touch(session, user.mid)
    items, _ = await NotifyService.list_for_user(
        session, user, page_num=page_num, page_size=page_size
    )
    return BiliSystemNotifyResp(
        data=SystemNotifyListResp(
            system_notify_list=[SystemNotifyItem.from_notify(i) for i in items]
        )
    )




@router.post("/read", response_model=StandardResponse[NotifyReadResp], summary="标记通知已读")
async def read_notify(
    session: SessionDep, user: RequiredUser, req: NotifyReadReq
) -> StandardResponse[NotifyReadResp]:
    """标记已读：传 notify_ids 精确标记，不传则全部已读。写入幂等。"""
    data = await NotifyService.mark_read(session, user, req.notify_ids)
    return StandardResponse(data=data)


@router.post("/delete", response_model=StandardResponse[int], summary="删除通知（仅管理员，逐用户软删）")
async def delete_notify(
    session: SessionDep, admin: AdminUser, req: NotifyReadReq
) -> StandardResponse[int]:
    """仅管理员可调用。

    普通用户不允许删除系统通知（系统通知面向全体、内容一致，用户只能标记已读）；
    删除需由管理员在管理界面操作。此处保留接口用于管理员按需清除指定用户的
    通知可见性（仅该用户不可见，不影响通知本体与其他用户）。
    """
    if not req.notify_ids:
        return StandardResponse(code=400, msg="notify_ids 不能为空")
    affected = await NotifyService.delete_for_user(session, admin, req.notify_ids)
    return StandardResponse(data=affected)


# ==================== 管理员侧 ====================


@router.post(
    "/admin/create",
    response_model=StandardResponse[NotifyAdminItem],
    summary="发布系统通知（管理员）",
)
async def create_notify(
    session: SessionDep, admin: AdminUser, req: NotifyCreateReq
) -> StandardResponse[NotifyAdminItem]:
    """发布一条系统通知。

    `publish_now=False` 存为草稿；`publish_at` 为未来时间即定时发布，
    到点后由后台任务自动投递推送（活跃用户实时推、非活跃用户批量推）。
    """
    data = await NotifyService.create(session, admin.mid, req)
    return StandardResponse(data=data)


@router.post(
    "/admin/update/{notify_id}",
    response_model=StandardResponse[NotifyAdminItem],
    summary="修改系统通知（管理员）",
)
async def update_notify(
    session: SessionDep, admin: AdminUser, notify_id: int, req: NotifyUpdateReq
) -> StandardResponse[NotifyAdminItem]:
    data = await NotifyService.update(session, notify_id, req)
    if data is None:
        return StandardResponse(code=404, msg=f"通知 {notify_id} 不存在")
    return StandardResponse(data=data)


@router.post(
    "/admin/revoke/{notify_id}",
    response_model=StandardResponse[bool],
    summary="撤回系统通知（管理员）",
)
async def revoke_notify(
    session: SessionDep, admin: AdminUser, notify_id: int
) -> StandardResponse[bool]:
    ok = await NotifyService.revoke(session, notify_id)
    if not ok:
        return StandardResponse(code=404, msg=f"通知 {notify_id} 不存在", data=False)
    return StandardResponse(data=True)


@router.get(
    "/admin/list",
    response_model=StandardResponse[NotifyAdminListResp],
    summary="通知列表（管理员）",
)
async def admin_list_notify(
    session: SessionDep,
    admin: AdminUser,
    page_num: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    status: NotifyStatusEnum | None = Query(default=None, description="按状态筛选"),
) -> StandardResponse[NotifyAdminListResp]:
    items, total = await NotifyService.admin_list(
        session, page_num=page_num, page_size=page_size, status=status
    )
    return StandardResponse(
        data=NotifyAdminListResp(
            items=items, total=total, page_num=page_num, page_size=page_size
        )
    )


__all__ = ["router"]
