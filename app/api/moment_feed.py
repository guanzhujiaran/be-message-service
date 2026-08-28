"""动态 Feed / 详情 HTTP 接口（/api/v1/dynamic）。

覆盖 P3-T6：

- GET /feed/all          综合页 Feed（仅 normal+未软删，pubTime 倒序，游标分页）
- GET /feed/following    关注流 Feed（仅登录用户关注的人的 normal 动态）
- GET /feed/space/{mid}  个人空间 Feed（本人=全部状态；访客=仅 normal；置顶优先）
- GET /detail/{dynId}    动态详情（权限过滤：非作者且非 normal → 404）
- POST /details          批量动态详情（限 20 条，权限过滤）

鉴权：Feed / 详情对未登录用户同样可读（viewer_mid 缺失时仅不展示点赞态）。
仅用 x-bili-mid 解析 viewer_mid（不强制登录）；**关注流 /feed/following
必须登录**（需知道"我"是谁），使用 RequiredUser。
"""

from fastapi import APIRouter, Header, Query

from app.models.str_int import StrInt
from loguru import logger

from app.core.database import SessionDep
from app.dependencies import RequiredUser
from app.models import StandardResponse
from app.models.schemas.moment import (
    MomentDetailResp,
    MomentDetailsReq,
    MomentFeedResp,
    MomentForwardListResp,
    MomentLikerListResp,
)
from app.services.user.follow import FollowService
from app.services.moment.moment_feed import MomentFeedService

router = APIRouter(prefix="/api/v1/community", tags=["moment-feed"])


async def _resolve_viewer(x_bili_mid: str | None) -> int | None:
    """解析可选登录态 viewer（未登录返回 None，不影响 Feed 读取）。"""
    if not x_bili_mid:
        return None
    try:
        mid = int(x_bili_mid)
    except (TypeError, ValueError):
        return None
    return mid if mid > 0 else None


_SHOWLIST_LIMIT = 100


def _parse_dyn_id_list(raw: str | None) -> list[int] | None:
    """解析逗号分隔 dynId 列表（去非法、去重、限 _SHOWLIST_LIMIT 个防滥用）。"""
    if not raw:
        return None
    result: list[int] = []
    seen: set[int] = set()
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            did = int(part)
        except (TypeError, ValueError):
            continue
        if did > 0 and did not in seen:
            seen.add(did)
            result.append(did)
            if len(result) >= _SHOWLIST_LIMIT:
                break
    return result or None


@router.get(
    "/feed/all",
    response_model=StandardResponse[MomentFeedResp],
    response_model_exclude_none=True,
    summary="综合页 Feed",
)
async def feed_all(
    session: SessionDep,
    x_bili_mid: str | None = Header(default=None),
    sort: str = Query(
        "recommend",
        description="排序：recommend=EdgeRank 推荐流（默认，无分页游标，last_showlist 去重）/ time=最新（pubTime 倒序 + historyOffset 游标）",
    ),
    ps: int = Query(default=20, ge=1, le=50, description="单页条数（对齐 B 站 ps，优先于 page_size）"),
    last_showlist: str | None = Query(
        default=None,
        description="已展示的 dynId 列表（逗号分隔，服务端去重，上限 100 个）",
    ),
    last_clicklist: str | None = Query(
        default=None,
        description="已互动的 dynId 列表（逗号分隔，预留反馈通道，当前不参与排序）",
    ),
    fresh_idx: int | None = Query(default=None, description="刷新序号（1 起递增，仅日志统计）"),
    fresh_idx_1h: int | None = Query(default=None, description="1 小时内刷新次数（仅日志统计）"),
    uniq_id: str | None = Query(default=None, description="客户端唯一 ID"),
    # ---- 2.46.0：地理位置召回（可选，未传则跳过该路）----
    lat: float | None = Query(default=None, ge=-90, le=90, description="[recommend 可选] 纬度（地理位置召回）"),
    lng: float | None = Query(default=None, ge=-180, le=180, description="[recommend 可选] 经度（地理位置召回）"),
    # ---- 以下参数为 time 模式保留 / 旧客户端兼容（recommend 模式忽略）----
    page: int = Query(default=1, ge=1, description="[recommend 忽略] 分页页码"),
    page_size: int = Query(default=20, ge=1, le=50, description="[兼容] 等价 ps，ps 优先"),
    update_baseline: int | None = Query(default=None),
    history_offset: int | None = Query(default=None),
    refresh_type: int = Query(default=1, description="1=刷新,2=翻页（recommend 忽略）"),
) -> StandardResponse[MomentFeedResp]:
    viewer = await _resolve_viewer(x_bili_mid)
    data = await MomentFeedService.comprehensive_feed(
        session,
        page=page,
        page_size=ps if ps else page_size,
        update_baseline=update_baseline,
        history_offset=history_offset,
        refresh_type=refresh_type,
        viewer_mid=viewer,
        sort=sort if sort in ("recommend", "time") else "recommend",
        last_showlist=_parse_dyn_id_list(last_showlist),
        last_clicklist=_parse_dyn_id_list(last_clicklist),
        uniq_id=uniq_id,
        lat=lat,
        lng=lng,
    )
    return StandardResponse(data=data)


@router.get(
    "/feed/following",
    response_model=StandardResponse[MomentFeedResp],
    response_model_exclude_none=True,
    summary="关注流 Feed（我关注的人的动态）",
)
async def feed_following(
    session: SessionDep,
    user: RequiredUser,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=50),
    history_offset: int | None = Query(default=None),
) -> StandardResponse[MomentFeedResp]:
    """关注流：仅展示当前登录用户**关注的人**发布的 normal 动态。

    关注 mid 取自 ``msg_user_follow``（FollowService.list_following_mids），
    动态过滤与综合页一致（normal + 未软删 + pubTime 非空），按 pubTime 倒序。
    必须登录（RequiredUser），未登录直接 401。
    """
    data = await MomentFeedService.following_feed(
        session,
        viewer_mid=user.mid,
        page=page,
        page_size=page_size,
        history_offset=history_offset,
    )
    return StandardResponse(data=data)


@router.get(
    "/feed/space/{mid}",
    response_model=StandardResponse[MomentFeedResp],
    response_model_exclude_none=True,
    summary="个人空间 Feed",
)
async def feed_space(
    session: SessionDep,
    mid: StrInt,
    x_bili_mid: str | None = Header(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=50),
    history_offset: int | None = Query(default=None),
) -> StandardResponse[MomentFeedResp]:
    if mid <= 0:
        return StandardResponse(code=400, msg="mid 不合法")
    viewer = await _resolve_viewer(x_bili_mid)
    # 黑名单互访拒绝（本人除外）：已拉黑目标或被目标拉黑均不可访问其空间
    if viewer is not None and viewer != mid:
        blocked = await FollowService.is_blocked_relation(session, viewer, mid)
        if blocked:
            return StandardResponse(code=403, msg="对方已将你加入黑名单，无法访问其空间")
    data = await MomentFeedService.space_feed(
        session,
        host_mid=mid,
        viewer_mid=viewer,
        page=page,
        page_size=page_size,
        history_offset=history_offset,
    )
    return StandardResponse(data=data)


@router.get(
    "/detail/{moment_id}",
    response_model=StandardResponse[MomentDetailResp],
    response_model_exclude_none=True,
    summary="动态详情",
)
async def detail(
    session: SessionDep,
    moment_id: StrInt,
    x_bili_mid: str | None = Header(default=None),
) -> StandardResponse[MomentDetailResp]:
    if moment_id <= 0:
        return StandardResponse(code=400, msg="dynId 不合法")
    viewer = await _resolve_viewer(x_bili_mid)
    data = await MomentFeedService.get_detail(session, moment_id, viewer_mid=viewer)
    if data is None:
        return StandardResponse(code=404, msg="动态不存在或暂不可见")
    # 浏览计数：2.41.0 起统一由 GET /interaction/status/{bizId}（detail 页必调）投递 MQ
    # 异步累计，此处不再同步上报（避免与 status 接口双计）。
    return StandardResponse(data=data)


@router.post(
    "/details",
    response_model=StandardResponse[list[MomentDetailResp]],
    response_model_exclude_none=True,
    summary="批量动态详情",
)
async def details_batch(
    session: SessionDep,
    req: MomentDetailsReq,
    x_bili_mid: str | None = Header(default=None),
) -> StandardResponse[list[MomentDetailResp]]:
    viewer = await _resolve_viewer(x_bili_mid)
    data = await MomentFeedService.get_details_batch(
        session, req.dynamicIds, viewer_mid=viewer
    )
    return StandardResponse(data=data)


# ==================== 点赞明细 / 转发列表（P8-T9）====================


@router.get(
    "/{moment_id}/likers",
    response_model=StandardResponse[MomentLikerListResp],
    response_model_exclude_none=True,
    summary="点赞明细列表（公开可读）",
)
async def get_moment_likers(
    session: SessionDep,
    moment_id: StrInt,
    page_num: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=50),
) -> StandardResponse[MomentLikerListResp]:
    """该动态的点赞用户列表（按点赞时间倒序，分页）。

    与 detail 接口一致：公开可读，匿名访客也能访问（与 P8-T7 一致的策略）。
    """
    data = await MomentFeedService.list_likers(
        session, moment_id, page_num=page_num, page_size=page_size
    )
    return StandardResponse(data=data)


@router.get(
    "/{moment_id}/forwards",
    response_model=StandardResponse[MomentForwardListResp],
    response_model_exclude_none=True,
    summary="转发该动态的动态列表（公开可读）",
)
async def get_moment_forwards(
    session: SessionDep,
    moment_id: StrInt,
    page_num: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=50),
) -> StandardResponse[MomentForwardListResp]:
    """转发该动态的 normal 动态列表（按 pubTime 倒序，分页）。

    与 detail 接口一致：公开可读，匿名访客也能访问。
    """
    data = await MomentFeedService.list_forwards(
        session, moment_id, page_num=page_num, page_size=page_size
    )
    return StandardResponse(data=data)


__all__ = ["router"]
