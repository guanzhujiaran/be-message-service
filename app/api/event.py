"""事件提醒模块 HTTP 接口（/api/v1/message/event）。

覆盖点赞 / 回复 / @提及三类互动提醒：

- `POST /report`     业务方（爬虫、抽奖、评论服务等）上报一条互动事件；
                     内部已做「消息设置闸门 + 幂等去重 + 活跃度分流」。
- `GET  /aggregate`  **消息聚合展示**：按 `source_type + source_id` 分组，
                     把「N 个人赞了同一条动态」收敛成一张卡片。
- `GET  /list`       点开聚合卡片后的明细列表。
- `POST /read`       已读，支持 id / 类型 / 聚合分组三种粒度。
- `GET  /unread`     各类型未读数（前端红点）。
"""

from fastapi import APIRouter, Query

from app.core.database import SessionDep
from app.dependencies import RequiredUser
from app.models import StandardResponse
from app.models.enums import EventTypeEnum, SourceTypeEnum
from app.models.schemas import (
    EventAggregateResp,
    EventListResp,
    EventReadReq,
    EventReadResp,
    EventReportReq,
    EventReportResp,
)
from app.services.activity import ActivityService
from app.services.event import EventService

router = APIRouter(prefix="/api/v1/message/event", tags=["message-event"])


@router.post(
    "/report", response_model=StandardResponse[EventReportResp], summary="上报互动事件"
)
async def report_event(
    session: SessionDep, req: EventReportReq
) -> StandardResponse[EventReportResp]:
    """上报一条点赞 / 回复 / @事件。

    内部依次经过：消息设置闸门 → 自赞过滤 → `dedup_key` 幂等 → 落库。
    事件提醒是站内信，落库即送达（接收方经 /list / msg_feed 轮询读取），
    不再做任何第三方渠道推送或实时 / 批量分流。重复上报只会返回
    `duplicated=True`，不会产生第二条提醒。

    本接口面向内部服务调用（不要求登录态），mid 由请求体显式指定。
    """
    data = await EventService.report(session, req)
    return StandardResponse(data=data)


@router.get(
    "/aggregate",
    response_model=StandardResponse[EventAggregateResp],
    summary="聚合展示互动提醒",
)
async def aggregate_event(
    session: SessionDep,
    user: RequiredUser,
    event_type: EventTypeEnum | None = Query(default=None, description="按类型筛选"),
    page_num: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=50),
    only_unread: bool = Query(default=False),
) -> StandardResponse[EventAggregateResp]:
    """按来源实体聚合的提醒卡片列表（消息中心首页）。"""
    await ActivityService.touch(session, user.mid)
    items, total = await EventService.aggregate(
        session,
        user.mid,
        event_type=event_type,
        page_num=page_num,
        page_size=page_size,
        only_unread=only_unread,
    )
    return StandardResponse(
        data=EventAggregateResp(
            items=items, total=total, page_num=page_num, page_size=page_size
        )
    )


@router.get(
    "/list", response_model=StandardResponse[EventListResp], summary="互动提醒明细列表"
)
async def list_event(
    session: SessionDep,
    user: RequiredUser,
    event_type: EventTypeEnum | None = Query(default=None),
    source_type: SourceTypeEnum | None = Query(default=None),
    source_id: str | None = Query(default=None, max_length=64),
    page_num: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=50),
    only_unread: bool = Query(default=False),
) -> StandardResponse[EventListResp]:
    """查看某个聚合分组下的事件明细（传 source_type + source_id 即可）。"""
    items, total = await EventService.list_detail(
        session,
        user.mid,
        event_type=event_type,
        source_type=source_type,
        source_id=source_id,
        page_num=page_num,
        page_size=page_size,
        only_unread=only_unread,
    )
    return StandardResponse(
        data=EventListResp(
            items=items, total=total, page_num=page_num, page_size=page_size
        )
    )


@router.post(
    "/read", response_model=StandardResponse[EventReadResp], summary="标记互动提醒已读"
)
async def read_event(
    session: SessionDep, user: RequiredUser, req: EventReadReq
) -> StandardResponse[EventReadResp]:
    """标记已读：

    - 传 `event_ids` → 精确已读；
    - 传 `event_type` → 该类型一键已读；
    - 再加 `source_type + source_id` → 只清掉某一张聚合卡片。
    """
    data = await EventService.mark_read(session, user.mid, req)
    return StandardResponse(data=data)


@router.post("/delete", response_model=StandardResponse[int], summary="删除互动提醒")
async def delete_event(
    session: SessionDep, user: RequiredUser, req: EventReadReq
) -> StandardResponse[int]:
    if not req.event_ids:
        return StandardResponse(code=400, msg="event_ids 不能为空")
    affected = await EventService.delete(session, user.mid, req.event_ids)
    return StandardResponse(data=affected)


@router.get(
    "/unread", response_model=StandardResponse[dict], summary="各类型互动未读数"
)
async def unread_event(session: SessionDep, user: RequiredUser) -> StandardResponse[dict]:
    """一次查询返回 like / reply / at 的未读数，供前端渲染红点。"""
    data = await EventService.count_unread_by_type(session, user.mid)
    return StandardResponse(
        data={
            "like": data.get(EventTypeEnum.LIKE.value, 0),
            "reply": data.get(EventTypeEnum.REPLY.value, 0),
            "at": data.get(EventTypeEnum.AT.value, 0),
            "total": sum(data.values()),
        }
    )


__all__ = ["router"]
