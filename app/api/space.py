"""社区空间行为时间线 / 浏览历史接口（/api/v1/community/space）。

- `GET /timeline`：行为时间线，我的主页与 TA 主页**共用**，后端按 viewer 身份
  用隐私开关整类过滤条目类型（关注 / 点赞 / 收藏）；
- `GET /fans`：TA 主页粉丝模块，按 `show_fans_list` 隐私开关门控，最多回看最新
  500 个粉丝；
- `GET /view-history`：我的浏览历史，**仅本人可读**（mid 必须等于当前登录用户）。
"""

from typing import Annotated

from fastapi import APIRouter, Header, Query

from app.core.database import SessionDep
from app.dependencies import CurrentUser
from app.models import StandardResponse
from app.models.str_int import StrInt
from app.models.schemas.follow import FollowListResp
from app.models.schemas.space import SpaceTimelineResp, SpaceViewHistoryResp
from app.services.user.follow import FollowService
from app.services.user.space_timeline import SpaceTimelineService
from app.services.user.space_view_history import SpaceViewHistoryService

router = APIRouter(prefix="/api/v1/community/space", tags=["community-space"])


def _resolve_viewer(x_bili_mid: str | None) -> int | None:
    """解析可选登录态 viewer（未登录返回 None，按 TA 主页公开口径过滤）。"""
    if not x_bili_mid:
        return None
    try:
        mid = int(x_bili_mid)
    except (TypeError, ValueError):
        return None
    return mid if mid > 0 else None


@router.get(
    "/timeline",
    response_model=StandardResponse[SpaceTimelineResp],
    summary="空间行为时间线（我的主页与 TA 主页共用）",
)
async def space_timeline(
    session: SessionDep,
    mid: Annotated[StrInt, Query(..., description="目标空间 mid（本人或 TA）")],
    cursor: str | None = Query(
        default=None, description="上一页返回的游标（ISO 时间）"
    ),
    page_size: int = Query(default=20, ge=1, le=50),
    x_bili_mid: str | None = Header(default=None, description="可选登录态 viewer mid"),
) -> StandardResponse[SpaceTimelineResp]:
    """行为时间线：关注 / 点赞 / 收藏三类条目按 acted_at 倒序。

    - viewer == mid（我的主页）：返回全部三类；
    - viewer != mid 或未登录（TA 主页）：按隐私开关整类过滤，关闭的类型不出现。
    """
    viewer = _resolve_viewer(x_bili_mid)
    data = await SpaceTimelineService.list(
        session, int(mid), viewer, cursor=cursor, page_size=page_size
    )
    return StandardResponse(data=data)


@router.get(
    "/fans",
    response_model=StandardResponse[FollowListResp],
    summary="空间粉丝列表（TA 主页粉丝模块）",
)
async def space_fans(
    session: SessionDep,
    mid: Annotated[StrInt, Query(..., description="目标空间 mid（查 TA 的粉丝）")],
    page_num: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    x_bili_mid: str | None = Header(default=None, description="可选登录态 viewer mid"),
) -> StandardResponse[FollowListResp]:
    """TA 的粉丝列表（按关注时间倒序）。

    - viewer == mid（主人看自己）：直接返回；
    - 访客或未登录：仅当 TA 打开 ``show_fans_list`` 才返回，否则整类返回空列表；
    - 最多回看最新 500 个粉丝，超出窗口的历史粉丝不再分页暴露。
    """
    viewer = _resolve_viewer(x_bili_mid)
    data = await FollowService.list_fans(
        session,
        int(mid),
        viewer,
        page_num=page_num,
        page_size=page_size,
    )
    return StandardResponse(data=data)


@router.get(
    "/view-history",
    response_model=StandardResponse[SpaceViewHistoryResp],
    summary="我的浏览历史",
)
async def space_view_history(
    session: SessionDep,
    user: CurrentUser,
    mid: Annotated[
        StrInt, Query(..., description="本人 mid（仅允许查自己的浏览历史）")
    ],
    cursor: str | None = Query(
        default=None, description="上一页返回的游标（ISO 时间）"
    ),
    page_size: int = Query(default=20, ge=1, le=50),
) -> StandardResponse[SpaceViewHistoryResp]:
    """我的浏览历史（按 last_view_at 倒序，仅本人可读）。"""
    if int(mid) != int(user.mid):
        return StandardResponse(code=403, msg="只能查看自己的浏览历史")
    data = await SpaceViewHistoryService.list(
        session, int(user.mid), cursor=cursor, page_size=page_size
    )
    return StandardResponse(data=data)


__all__ = ["router"]
