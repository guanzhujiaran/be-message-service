"""消息系统聚合入口（/api/v1/message/msg_feed）。

四个功能模块各自有独立路由（notify / event / dm / setting），
这里只提供**跨模块的聚合能力**，避免前端为了渲染一个红点连发四个请求：

- `GET /unread`    一次拿到系统通知 / 点赞 / 回复 / @ / 私信的全部未读数。
- `POST /heartbeat` 上报一次活跃心跳，用于推送策略分流
                    （活跃用户实时推送，非活跃用户定时批量推送）。
"""

from fastapi import APIRouter

from app.core.database import SessionDep
from app.dependencies import RequiredUser
from app.models import StandardResponse
from app.models.enums import EventTypeEnum
from app.models.schemas import EventUnreadResp, UserActivityResp
from app.services.activity import ActivityService
from app.services.dm import DmService
from app.services.event import EventService
from app.services.notify import NotifyService

router = APIRouter(prefix="/api/v1/message/msg_feed", tags=["message"])


@router.get(
    "/unread", response_model=StandardResponse[EventUnreadResp], summary="全站未读数汇总"
)
async def unread_summary(
    session: SessionDep, user: RequiredUser
) -> StandardResponse[EventUnreadResp]:
    """一次返回消息中心所有模块的未读数（前端顶部红点）。"""
    event_map = await EventService.count_unread_by_type(session, user.mid)
    notify_unread = await NotifyService.unread_count(session, user)
    dm_unread = await DmService.count_unread(session, user.mid)

    data = EventUnreadResp(
        like=event_map.get(EventTypeEnum.LIKE.value, 0),
        reply=event_map.get(EventTypeEnum.REPLY.value, 0),
        at=event_map.get(EventTypeEnum.AT.value, 0),
        notify=notify_unread,
        dm=dm_unread,
    )
    data.total = data.like + data.reply + data.at + data.notify + data.dm
    return StandardResponse(data=data)


@router.post(
    "/heartbeat",
    response_model=StandardResponse[UserActivityResp],
    summary="上报活跃心跳",
)
async def heartbeat(
    session: SessionDep, user: RequiredUser
) -> StandardResponse[UserActivityResp]:
    """前端在消息中心保持轮询时调用，把用户标记为活跃。

    活跃用户的提醒走实时推送；停止心跳超过 `active_user_window_seconds`
    后自动降级为「非活跃」，后续提醒由定时任务聚合成一条批量推送。
    """
    await ActivityService.touch(session, user.mid)
    return StandardResponse(data=await ActivityService.get_snapshot(session, user.mid))


__all__ = ["router"]
