"""消息设置模块 HTTP 接口（/api/v1/message/setting）。

用户可自定义是否接收「点赞 / 回复 / @提及 / 陌生人私信 / 系统通知」，
并可设置免打扰时段。这份设置是整个消息系统的第一道闸门：
事件上报、私信投递、通知推送在落库或推送前都会先查这里。
"""

from fastapi import APIRouter

from app.core.database import SessionDep
from app.dependencies import RequiredUser
from app.models import StandardResponse
from app.models.schemas import (
    MessageSettingResp,
    MessageSettingUpdateReq,
    UserActivityResp,
)
from app.services.activity import ActivityService
from app.services.setting import SettingService

router = APIRouter(prefix="/api/v1/message/setting", tags=["message-setting"])


@router.get(
    "", response_model=StandardResponse[MessageSettingResp], summary="获取消息设置"
)
async def get_setting(
    session: SessionDep, user: RequiredUser
) -> StandardResponse[MessageSettingResp]:
    """获取当前用户的消息设置，首次访问时按「全部开启」自动初始化。"""
    return StandardResponse(data=await SettingService.get(session, user.mid))


@router.post(
    "/update",
    response_model=StandardResponse[MessageSettingResp],
    summary="更新消息设置",
)
async def update_setting(
    session: SessionDep, user: RequiredUser, req: MessageSettingUpdateReq
) -> StandardResponse[MessageSettingResp]:
    """部分更新：只写入本次显式传入的字段，未传字段保持原值。

    关闭某类开关后，对应提醒会在**上报阶段就被短路**，不会落库也不会推送。
    """
    return StandardResponse(data=await SettingService.update(session, user.mid, req))


@router.get(
    "/activity",
    response_model=StandardResponse[UserActivityResp],
    summary="查看自身活跃度快照",
)
async def get_activity(
    session: SessionDep, user: RequiredUser
) -> StandardResponse[UserActivityResp]:
    """活跃度快照：`is_active=True` 的用户走实时推送，否则进批量聚合推送。"""
    return StandardResponse(data=await ActivityService.get_snapshot(session, user.mid))


__all__ = ["router"]
