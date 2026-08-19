"""动态发布 / 互动 / 话题&@&POI 类 HTTP 接口（/api/v1/moment）。

覆盖 P2-T7（发布）+ P4-T7（互动）+ P5-T7（话题 / @ / POI）：

发布类：
- POST /create        创建动态（WORD / FORWARD），响应 auditStatus='auditing'
- POST /edit          编辑动态（rejected / auditing 编辑后回 auditing）
- POST /remove        删除动态（软删，仅作者本人）
- POST /admin/remove  管理员删除动态（软删，2.22.1，仅 root，可删任意动态）
- POST /repost        转发动态（FORWARD）
- POST /space/top     空间置顶（仅本人 + normal）
- POST /space/untop   取消置顶
- POST /create/check  发布页预校验

互动类（P4-T7）：
- POST /thumb         点赞 / 取消点赞（幂等）
- POST /report        举报动态（不改 auditStatus）
（浏览计数无上报接口：由后端在详情接口 GET /detail/{id} 访问时自动累计）

话题 / @ / POI 辅助类（P5-T7）：
- GET /topic/square       话题广场列表
- GET /topic/hot-search   热门话题
- GET /topic/feed/{id}    话题动态流
- GET /at/list            @用户推荐（关注 / 粉丝分组）
- GET /at/search          @用户搜索（昵称模糊匹配）
- GET /poi/nearby         附近地点（MVP 本地模式）
- GET /poi/search         POI 关键词搜索（MVP 本地模式）

鉴权复用 `RequiredUser`（JWT → x-bili-mid）。所有写接口以 `ValueError` 抛业务错误，
由统一异常中间件转标准响应；路由层只负责 400/403/404 的语义映射。
"""

import asyncio
from datetime import datetime

from sqlmodel import select, col, func
from fastapi import APIRouter, Header, Query, Request

from app.core.database import SessionDep
from app.dependencies import RequiredUser, RootUser
from app.models import StandardResponse
from app.models.db import CommentSubject, TMoment, TMomentFavorite, TMomentLike, TMomentStat
from app.models.enums import (
    CommentTypeEnum,
    InteractionBizTypeEnum,
    MomentAuditStatusEnum,
)
from app.models.schemas.interaction import (
    InteractionStatusItem,
    InteractionStatusResp,
)
from app.models.schemas.mq import InteractionViewPayload
from app.models.schemas.moment import (
    MomentAtListResp,
    MomentAtSearchResp,
    MomentCreateCheckReq,
    MomentCreateCheckResp,
    MomentCreateReq,
    MomentCreateResp,
    MomentEditReq,
    MomentEditResp,
    MomentPoiResp,
    MomentRemoveReq,
    MomentRemoveResp,
    MomentReportReq,
    MomentReportResp,
    MomentRepostReq,
    MomentRepostResp,
    MomentThumbReq,
    MomentThumbResp,
    MomentTopicCreateReq,
    MomentTopicCreateResp,
    MomentTopicFeedResp,
    MomentTopicMineResp,
    MomentTopicSquareResp,
    MomentTopReq,
    MomentTopResp,
    MomentUpStatResp,
    MomentTopicDetailResp,
)
from app.services.follow import FollowService
from app.services.interaction import (
    BeMessageInteractionStatService as InteractionStatService,
)
from app.services.publisher import publish_interaction_view
from app.services.rpa_rpc import rpa_rpc_client
from app.services.moment_feed import MomentFeedService
from app.services.moment_interaction import MomentInteractionService
from app.services.moment_publish import MomentPublishService
from app.services.moment_topic import MomentTopicService
from app.utils.ip_mask import extract_client_ip

router = APIRouter(prefix="/api/v1/moment", tags=["moment"])


def _client_ctx(
    request: Request, user_agent: str | None
) -> tuple[str | None, str | None]:
    """提取客户端 IP / UA，供审核日志记录。"""
    ip_v4, ip_v6 = extract_client_ip(
        dict(request.headers),
        request.client.host if request.client else None,
    )
    return ip_v4 or ip_v6, user_agent


@router.post(
    "/create", response_model=StandardResponse[MomentCreateResp], summary="创建动态"
)
async def create_dynamic(
    session: SessionDep,
    user: RequiredUser,
    req: MomentCreateReq,
    request: Request,
    user_agent: str | None = Header(default=None, alias="user-agent"),
) -> StandardResponse[MomentCreateResp]:
    ip, ua = _client_ctx(request, user_agent)
    try:
        data = await MomentPublishService.create(
            session, user.mid, req, client_ip=ip, user_agent=ua
        )
    except ValueError as e:
        return StandardResponse(code=400, msg=str(e))
    return StandardResponse(data=MomentCreateResp(**data))


@router.post(
    "/edit", response_model=StandardResponse[MomentEditResp], summary="编辑动态"
)
async def edit_dynamic(
    session: SessionDep,
    user: RequiredUser,
    req: MomentEditReq,
    request: Request,
    user_agent: str | None = Header(default=None, alias="user-agent"),
) -> StandardResponse[MomentEditResp]:
    ip, ua = _client_ctx(request, user_agent)
    try:
        data = await MomentPublishService.edit(
            session, user.mid, req, client_ip=ip, user_agent=ua
        )
    except ValueError as e:
        return StandardResponse(code=400, msg=str(e))
    return StandardResponse(data=MomentEditResp(**data))


@router.post(
    "/remove", response_model=StandardResponse[MomentRemoveResp], summary="删除动态"
)
async def remove_dynamic(
    session: SessionDep,
    user: RequiredUser,
    req: MomentRemoveReq,
    request: Request,
    user_agent: str | None = Header(default=None, alias="user-agent"),
) -> StandardResponse[MomentRemoveResp]:
    ip, ua = _client_ctx(request, user_agent)
    try:
        data = await MomentPublishService.remove(
            session, user.mid, req, client_ip=ip, user_agent=ua
        )
    except ValueError as e:
        return StandardResponse(code=400, msg=str(e))
    return StandardResponse(data=MomentRemoveResp(**data))


@router.post(
    "/admin/remove",
    response_model=StandardResponse[MomentRemoveResp],
    summary="管理员删除动态",
)
async def admin_remove_dynamic(
    session: SessionDep,
    user: RootUser,
    req: MomentRemoveReq,
    request: Request,
    user_agent: str | None = Header(default=None, alias="user-agent"),
) -> StandardResponse[MomentRemoveResp]:
    """管理员删除任意动态（2.22.1，仅 root）。

    与 `/remove` 相同的软删语义与状态机触发点 ④（FORWARD ∧ before=normal → 源动态
    repostCount -1），但跳过作者校验；AuditLog 以 `operatorRole=admin` + `remark='管理员删除'` 落库。
    """
    ip, ua = _client_ctx(request, user_agent)
    try:
        data = await MomentPublishService.remove(
            session,
            user.mid,
            req,
            operator_role="admin",
            client_ip=ip,
            user_agent=ua,
        )
    except ValueError as e:
        return StandardResponse(code=400, msg=str(e))
    return StandardResponse(data=MomentRemoveResp(**data))


@router.post(
    "/repost", response_model=StandardResponse[MomentRepostResp], summary="转发动态"
)
async def repost_dynamic(
    session: SessionDep,
    user: RequiredUser,
    req: MomentRepostReq,
    request: Request,
    user_agent: str | None = Header(default=None, alias="user-agent"),
) -> StandardResponse[MomentRepostResp]:
    ip, ua = _client_ctx(request, user_agent)
    try:
        data = await MomentPublishService.repost(
            session, user.mid, req, client_ip=ip, user_agent=ua
        )
    except ValueError as e:
        return StandardResponse(code=400, msg=str(e))
    return StandardResponse(data=MomentRepostResp(**data))


@router.post(
    "/space/top", response_model=StandardResponse[MomentTopResp], summary="空间置顶"
)
async def top_dynamic(
    session: SessionDep,
    user: RequiredUser,
    req: MomentTopReq,
) -> StandardResponse[MomentTopResp]:
    try:
        data = await MomentPublishService.top(session, user.mid, req, untop=False)
    except ValueError as e:
        return StandardResponse(code=400, msg=str(e))
    return StandardResponse(data=MomentTopResp(**data))


@router.post(
    "/space/untop", response_model=StandardResponse[MomentTopResp], summary="取消置顶"
)
async def untop_dynamic(
    session: SessionDep,
    user: RequiredUser,
    req: MomentTopReq,
) -> StandardResponse[MomentTopResp]:
    try:
        data = await MomentPublishService.top(session, user.mid, req, untop=True)
    except ValueError as e:
        return StandardResponse(code=400, msg=str(e))
    return StandardResponse(data=MomentTopResp(**data))


@router.post(
    "/create/check",
    response_model=StandardResponse[MomentCreateCheckResp],
    summary="发布页预校验",
)
async def create_check(
    user: RequiredUser,
    req: MomentCreateCheckReq,
) -> StandardResponse[MomentCreateCheckResp]:
    try:
        data = await MomentPublishService.create_check(user.mid, req.scene)
    except ValueError as e:
        return StandardResponse(code=400, msg=str(e))
    return StandardResponse(data=MomentCreateCheckResp(**data))


# ==================== 互动类（P4-T7）====================


@router.post(
    "/thumb",
    response_model=StandardResponse[MomentThumbResp],
    summary="点赞 / 取消点赞",
)
async def thumb(
    session: SessionDep,
    user: RequiredUser,
    req: MomentThumbReq,
) -> StandardResponse[MomentThumbResp]:
    # 解析目标资源 (bizType, bizId)：dynamic 时 bizId 与 dynId 任取其一
    try:
        biz_type = InteractionBizTypeEnum.from_text(req.bizType)
    except (ValueError, KeyError):
        return StandardResponse(code=400, msg=f"不支持的资源类型: {req.bizType}")
    if biz_type == InteractionBizTypeEnum.DYNAMIC:
        biz_id = req.bizId if req.bizId is not None else req.dynId
        if biz_id is None:
            return StandardResponse(code=400, msg="bizId/dynId 不合法")
    else:
        if req.bizId is None:
            return StandardResponse(code=400, msg="bizId 必填")
        biz_id = req.bizId
    try:
        is_like, like_count = await MomentInteractionService.thumb(
            session, user.mid, biz_type, biz_id, req.up
        )
    except ValueError as e:
        return StandardResponse(code=400, msg=str(e))
    return StandardResponse(
        data=MomentThumbResp(
            bizType=biz_type.to_text(),
            bizId=biz_id,
            bizIdStr=str(biz_id),
            dynId=req.dynId,
            dynIdStr=str(req.dynId) if req.dynId is not None else None,
            isLike=is_like,
            likeCount=like_count,
        )
    )


@router.get(
    "/interaction/status",
    response_model=StandardResponse[InteractionStatusResp],
    summary="批量查询某类型资源当前用户收藏/点赞态 + 计数",
)
async def interaction_status(
    session: SessionDep,
    user: RequiredUser,
    bizType: str = Query(description="资源类型（文字：dynamic/lottery/...）"),
    bizIds: str = Query(description="资源 id 列表（逗号分隔，限 50 个）"),
) -> StandardResponse[InteractionStatusResp]:
    try:
        bizType = InteractionBizTypeEnum.from_text(bizType)
    except (ValueError, KeyError):
        return StandardResponse(code=400, msg=f"不支持的资源类型: {bizType}")
    try:
        ids = [int(x.strip()) for x in bizIds.split(",") if x.strip()]
    except ValueError:
        return StandardResponse(code=400, msg="bizIds 不合法")
    if not ids:
        return StandardResponse(code=400, msg="bizIds 不能为空")
    ids = ids[:50]
    mid = user.mid

    # 计数：动态走 TMomentStat，非动态走 TInteractionStat
    if InteractionStatService.is_dynamic(bizType):
        like_counts: dict[int, int] = {}
        fav_counts: dict[int, int] = {}
        stats = (
            await session.exec(
                select(TMomentStat).where(col(TMomentStat.dynId).in_(ids))
            )
        ).all()
        stats_by_id: dict[int, TMomentStat] = {s.dynId: s for s in stats}
        comment_counts = {s.dynId: int(s.commentCount) for s in stats}
        repost_counts = {s.dynId: int(s.repostCount) for s in stats}
        for s in stats:
            like_counts[s.dynId] = int(s.likeCount)
            fav_counts[s.dynId] = int(s.favoriteCount)
        for _id in ids:
            like_counts.setdefault(_id, 0)
            fav_counts.setdefault(_id, 0)
        view_counts = {s.dynId: int(s.viewCount) for s in stats}
    else:
        counts = await InteractionStatService.batch_get_counts(session, bizType, ids)
        like_counts = {b: c["likeCount"] for b, c in counts.items()}
        fav_counts = {b: c["favoriteCount"] for b, c in counts.items()}
        view_counts = {b: c["viewCount"] for b, c in counts.items()}
        # 评论数：lottery 走 be-message 评论系统（CommentSubject.all_count 冗余计数），
        # rpa_* 无评论功能 → 恒 0
        comment_counts: dict[int, int] = {}
        if bizType == InteractionBizTypeEnum.LOTTERY:
            subjects = (
                await session.exec(
                    select(CommentSubject).where(
                        col(CommentSubject.type) == CommentTypeEnum.LOTTERY,
                        col(CommentSubject.oid).in_(ids),
                    )
                )
            ).all()
            comment_counts = {s.oid: int(s.all_count) for s in subjects}
        # 转发数：引用该资源生成的动态数（TMoment.bizType/bizRid 可见动态，转发到动态时写入）
        rows = (
            await session.exec(
                select(TMoment.bizRid, func.count())
                .where(
                    col(TMoment.bizType) == bizType,
                    col(TMoment.bizRid).in_(ids),
                    col(TMoment.deletedAt).is_(None),
                    col(TMoment.auditStatus) == MomentAuditStatusEnum.NORMAL,
                )
                .group_by(TMoment.bizRid)
            )
        ).all()
        repost_counts: dict[int, int] = {int(r): int(c) for r, c in rows}

    # 当前用户点赞态 / 收藏态
    liked_ids = set(
        (
            await session.exec(
                select(TMomentLike.bizId).where(
                    col(TMomentLike.bizType) == bizType,
                    col(TMomentLike.bizId).in_(ids),
                    col(TMomentLike.mid) == mid,
                )
            )
        ).all()
    )
    faved_ids = set(
        (
            await session.exec(
                select(TMomentFavorite.bizId).where(
                    col(TMomentFavorite.bizType) == bizType,
                    col(TMomentFavorite.bizId).in_(ids),
                    col(TMomentFavorite.mid) == mid,
                )
            )
        ).all()
    )

    # 非动态资源（RPA 等）详情经 RPC 从资源归属服务获取（弱依赖，失败 detail=None）
    details: dict[int, object] = {}
    if not InteractionStatService.is_dynamic(bizType):
        results = await asyncio.gather(
            *[rpa_rpc_client.get_resource_detail(bizType.to_text(), _id) for _id in ids],
            return_exceptions=True,
        )
        for _id, res in zip(ids, results):
            detail = None
            if isinstance(res, Exception):
                detail = None
            elif res is not None and getattr(res, "detail", None) is not None:
                detail = res.detail
            details[_id] = detail

    items = [
        InteractionStatusItem(
            bizType=bizType.to_text(),
            bizId=str(_id),
            isLike=_id in liked_ids,
            isFavorite=_id in faved_ids,
            likeCount=like_counts.get(_id, 0),
            favoriteCount=fav_counts.get(_id, 0),
            commentCount=comment_counts.get(_id, 0),
            repostCount=repost_counts.get(_id, 0),
            viewCount=view_counts.get(_id, 0),
            detail=details.get(_id),
        )
        for _id in ids
    ]

    # 浏览统计触发点（2.23.0）：本接口为前端页面拉互动态的必调入口，
    # 仅登录用户，对列表内每个资源投递 MQ（interaction.view 队列）异步去重累计：
    # 主链路不阻塞、投递失败静默不拖慢 status，由消费者独立重试；
    # 确保计数来自真实前端页面访问而非直接调 API（不提供前端主动上报接口）。
    ref_date = datetime.now().strftime("%Y-%m-%d")
    for _id in ids:
        await publish_interaction_view(
            InteractionViewPayload(
                bizType=bizType.to_text(), bizId=str(_id), mid=mid, refDate=ref_date
            )
        )
    return StandardResponse(data=InteractionStatusResp(items=items))


@router.post(
    "/report",
    response_model=StandardResponse[MomentReportResp],
    summary="举报动态",
)
async def report(
    session: SessionDep,
    user: RequiredUser,
    req: MomentReportReq,
) -> StandardResponse[MomentReportResp]:
    try:
        await MomentInteractionService.report(session, user.mid, req)
    except ValueError as e:
        return StandardResponse(code=400, msg=str(e))
    return StandardResponse(
        data=MomentReportResp(dynId=req.dynId, dynIdStr=str(req.dynId))
    )


# ==================== 话题 & @ & POI（P5-T7）====================


@router.get(
    "/topic/square",
    response_model=StandardResponse[MomentTopicSquareResp],
    summary="话题广场",
)
async def topic_square(
    session: SessionDep,
    user: RequiredUser,
    page: int = 1,
    page_size: int = 20,
) -> StandardResponse[MomentTopicSquareResp]:
    data = await MomentTopicService.topic_square(
        session, page=page, page_size=page_size
    )
    return StandardResponse(data=data)


@router.get(
    "/topic/hot-search",
    response_model=StandardResponse[MomentTopicSquareResp],
    summary="热门话题",
)
async def topic_hot_search(
    session: SessionDep,
    user: RequiredUser,
    page: int = 1,
    page_size: int = 20,
) -> StandardResponse[MomentTopicSquareResp]:
    data = await MomentTopicService.topic_square(
        session, page=page, page_size=page_size, hot_only=True
    )
    return StandardResponse(data=data)


@router.get(
    "/topic/detail/{topicId}",
    response_model=StandardResponse[MomentTopicDetailResp],
    response_model_exclude_none=True,
    summary="话题详情（对齐 B 站 top_details）",
)
async def topic_detail(
    session: SessionDep,
    user: RequiredUser,
    topicId: int,
) -> StandardResponse[MomentTopicDetailResp]:
    data = await MomentTopicService.topic_detail(
        session, topic_id=topicId, viewer_mid=user.mid
    )
    if data is None:
        return StandardResponse(code=404, msg="话题不存在")
    return StandardResponse(data=data)


@router.get(
    "/topic/feed/{topicId}",
    response_model=StandardResponse[MomentTopicFeedResp],
    response_model_exclude_none=True,
    summary="话题动态流（支持热门/最新排序）",
)
async def topic_feed(
    session: SessionDep,
    user: RequiredUser,
    topicId: int,
    sort: str = Query(
        "hot", description="排序：hot=热门（互动数倒序）/ time=最新（发布时间倒序）"
    ),
    history_offset: int | None = None,
    page: int = 1,
    page_size: int = 20,
) -> StandardResponse[MomentTopicFeedResp]:
    data = await MomentFeedService.topic_feed(
        session,
        topic_id=topicId,
        page=page,
        page_size=page_size,
        viewer_mid=user.mid,
        history_offset=history_offset,
        sort=sort,
    )
    return StandardResponse(data=data)


@router.post(
    "/topic/create",
    response_model=StandardResponse[MomentTopicCreateResp],
    summary="创建话题（创建即进入审核）",
)
async def topic_create(
    session: SessionDep,
    user: RequiredUser,
    req: MomentTopicCreateReq,
) -> StandardResponse[MomentTopicCreateResp]:
    try:
        data = await MomentTopicService.create_topic(session, mid=user.mid, req=req)
    except ValueError as e:
        return StandardResponse(code=422, msg=str(e))
    return StandardResponse(data=data, msg="话题已提交审核")


@router.get(
    "/topic/mine",
    response_model=StandardResponse[MomentTopicMineResp],
    summary="我创建的话题（含审核状态）",
)
async def topic_mine(
    session: SessionDep,
    user: RequiredUser,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=50),
) -> StandardResponse[MomentTopicMineResp]:
    data = await MomentTopicService.mine(
        session, mid=user.mid, page_num=page, page_size=page_size
    )
    return StandardResponse(data=data)


@router.get(
    "/at/list",
    response_model=StandardResponse[MomentAtListResp],
    summary="@用户推荐列表",
)
async def at_list(
    session: SessionDep,
    user: RequiredUser,
    page_size: int = 20,
) -> StandardResponse[MomentAtListResp]:
    data = await MomentTopicService.at_recommend(session, user.mid, page_size=page_size)
    return StandardResponse(data=data)


@router.get(
    "/at/search",
    response_model=StandardResponse[MomentAtSearchResp],
    summary="@用户搜索",
)
async def at_search(
    user: RequiredUser,
    keyword: str = "",
    page_size: int = 20,
) -> StandardResponse[MomentAtSearchResp]:
    data = await MomentTopicService.at_search(keyword, page_size=page_size)
    return StandardResponse(data=data)


@router.get(
    "/poi/nearby",
    response_model=StandardResponse[MomentPoiResp],
    summary="附近地点",
)
async def poi_nearby(
    session: SessionDep,
    user: RequiredUser,
    lat: float | None = None,
    lng: float | None = None,
    page: int = 1,
    page_size: int = 20,
) -> StandardResponse[MomentPoiResp]:
    data = await MomentTopicService.poi_nearby(
        session, lat=lat, lng=lng, page=page, page_size=page_size
    )
    return StandardResponse(data=data)


@router.get(
    "/poi/search",
    response_model=StandardResponse[MomentPoiResp],
    summary="POI 关键词搜索",
)
async def poi_search(
    session: SessionDep,
    user: RequiredUser,
    keyword: str = "",
    lat: float | None = None,
    lng: float | None = None,
    page: int = 1,
    page_size: int = 20,
) -> StandardResponse[MomentPoiResp]:
    data = await MomentTopicService.poi_search(
        session, keyword, lat=lat, lng=lng, page=page, page_size=page_size
    )
    return StandardResponse(data=data)


# ==================== 空间统计（对标 B 站 upstat）====================


@router.get(
    "/upstat",
    response_model=StandardResponse[MomentUpStatResp],
    summary="查询指定用户的空间统计（动态数 / 获赞数）",
)
async def get_upstat(
    session: SessionDep,
    vmid: int = Query(..., description="目标用户 mid（对标 B 站 vmid 参数）"),
    x_bili_mid: str | None = Header(default=None),
) -> StandardResponse[MomentUpStatResp]:
    """获取任意用户对外可见动态的总数与获赞总数（公开接口，无需登录）。

    对标 B 站 `https://api.bilibili.com/x/space/upstat?mid=`，
    返回该用户 ``dynamic_count``（动态数）与 ``like_count``（获赞数）。
    黑名单互访拒绝（本人除外）：与目标存在任一向黑名单关系时返回 403。
    """
    if vmid <= 0:
        return StandardResponse(code=400, msg="mid 不合法")
    viewer = None
    if x_bili_mid:
        try:
            viewer = int(x_bili_mid)
        except (TypeError, ValueError):
            viewer = None
    if viewer is not None and viewer != vmid:
        blocked = await FollowService.is_blocked_relation(session, viewer, vmid)
        if blocked:
            return StandardResponse(
                code=403, msg="对方已将你加入黑名单，无法访问其空间"
            )
    stat = await MomentFeedService.get_upstat(session, vmid)
    return StandardResponse(data=MomentUpStatResp(mid=vmid, **stat))
