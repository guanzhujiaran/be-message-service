"""系统通知模块 HTTP 接口（/api/v1/message/notify）。

面向两类调用方：

- **管理员**（role=root）：发布 / 修改 / 撤回通知，可按用户类型（全体 / 角色 /
  等级 / 大会员 / 指定 mid）投放，支持定时发布与过期时间。
- **普通用户**：定时拉取增量（游标语义，天然避免重复消费）、分页查看历史。
  **读取即已读**：`/pull` / `/list` / `/system` 在返回前自动把本页通知置为已读，
  因此不提供（也不需要）单独的「标记已读」接口。
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
from app.models.str_int import StrInt
from app.models.schemas import (
    BiliSystemNotifyResp,
    NotifyAdminItem,
    NotifyAdminListResp,
    NotifyCreateReq,
    NotifyDeleteReq,
    NotifyListResp,
    NotifyPullResp,
    NotifyUpdateReq,
    SystemNotifyItem,
    SystemNotifyListResp,
)
from app.services.message.insite.activity import ActivityService
from app.services.message.insite.notify import NotifyService

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

    本批通知在返回时即被自动标记为已读（读取即已读），响应的 `unread_count`
    是标记后的剩余未读数。
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
) -> StandardResponse[NotifyListResp]:
    """分页查看历史通知（不推进拉取游标），返回前本页通知自动置为已读。

    出参 `is_read` 是本次读取前的快照，可据此高亮「本次新到」的通知。
    """
    items, total = await NotifyService.list_for_user(
        session, user, page_num=page_num, page_size=page_size
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


@router.post("/delete", response_model=StandardResponse[int], summary="删除通知（仅管理员，逐用户软删）")
async def delete_notify(
    session: SessionDep, admin: AdminUser, req: NotifyDeleteReq
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

    与对外 RPC `message.notify.rpc.publish_notify`（见 `app.mq.rpc_notify`）落到
    同一个执行体 `NotifyService.create`：HTTP 面向管理员浏览器侧、RPC 面向其它
    服务端系统，二者不再有各自的实现副本。差别仅在是否满足幂等——管理端是人工
    显式操作，不做 `(target_value, title)` 判重；RPC 走 `create_idempotent`，
    便于调用方超时重试而不重复打扰用户。
    """
    data = await NotifyService.create(session, admin.mid, req)
    return StandardResponse(data=data)


@router.post(
    "/admin/update/{notify_id}",
    response_model=StandardResponse[NotifyAdminItem],
    summary="修改系统通知（管理员）",
)
async def update_notify(
    session: SessionDep, admin: AdminUser, notify_id: StrInt, req: NotifyUpdateReq
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
    session: SessionDep, admin: AdminUser, notify_id: StrInt
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
