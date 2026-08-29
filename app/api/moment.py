"""动态发布 / 互动 / 话题&@&POI 类 HTTP 接口（/api/v1/community）。

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

互动类（P4-T7，2.47.0 起直接实例化 `interaction_actions` 操作对象）：
- POST /thumb         点赞 / 取消点赞（幂等，LikeAction）
- POST /dislike       点踩 / 取消点踩（幂等，DislikeAction）
- POST /share         分享上报（shareCount +1，ShareAction）
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

from sqlmodel import select, col, func
from typing import Annotated

from fastapi import APIRouter, Header, Query, Request

from app.models.str_int import StrInt

from app.core.database import SessionDep
from app.dependencies import RequiredUser, RootUser
from app.models import StandardResponse
from app.models.db import (
    CommentSubject,
    TMoment,
    TMomentFavorite,
    TMomentLike,
    TResourceReport,
)
from app.models.enums import (
    CommentTypeEnum,
    InteractionBizTypeEnum,
    MomentAuditStatusEnum,
)
from bili_common.models.report import ReportBizTypeEnum
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
    MomentDislikeReq,
    MomentDislikeResp,
    MomentEditReq,
    MomentEditResp,
    MomentPoiResp,
    MomentRemoveReq,
    MomentRemoveResp,
    MomentReportReq,
    MomentReportResp,
    MomentRepostReq,
    MomentRepostResp,
    MomentShareReq,
    MomentShareResp,
    MomentThumbReq,
    MomentThumbResp,
    MomentTopicCreateReq,
    MomentTopicCreateResp,
    MomentTopicFeedResp,
    MomentTopicMineResp,
    MomentTopicSquareResp,
    MomentTopReq,
    MomentTopResp,
    MomentTopicDetailResp,
)
from app.services.user.follow import FollowService
from app.services.moment.interaction import (
    BeMessageInteractionStatService as InteractionStatService,
)
from app.services.message.infrastructure.publisher import publish_interaction_view
from app.services.infrastructure.rpa_rpc import rpa_rpc_client
from app.services.interaction_actions import (
    DislikeAction,
    ReportAction,
    RepostAction,
    ShareAction,
    get_action,
)
from app.services.moment.moment_feed import MomentFeedService
from app.services.moment.moment_publish import MomentPublishService
from app.services.moment.moment_topic import MomentTopicService
from app.utils.ip_mask import extract_client_ip

router = APIRouter(prefix="/api/v1/community", tags=["moment"])


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
        action = RepostAction(
            session,
            actor_mid=user.mid,
            biz_id=req.srcDynId,
            content=req.content,
            client_ip=ip,
            user_agent=ua,
        )
        data = await action.run()
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
    biz_type = req.bizType
    if biz_type == InteractionBizTypeEnum.DYNAMIC:
        biz_id = req.bizId if req.bizId is not None else req.dynId
        if biz_id is None:
            return StandardResponse(code=400, msg="bizId/dynId 不合法")
    else:
        if req.bizId is None:
            return StandardResponse(code=400, msg="bizId 必填")
        biz_id = req.bizId
    try:
        # 按 biz_type 分发到对应资源类型的点赞操作类（每个类声明自己的 _biz_type）
        action = get_action("like", biz_type)(
            session,
            actor_mid=user.mid,
            biz_id=biz_id,
            up=req.up,
            dyn_id=req.dynId,
        )
        is_like, like_count = await action.run()
    except ValueError as e:
        return StandardResponse(code=400, msg=str(e))
    return StandardResponse(
        data=MomentThumbResp(
            bizType=biz_type,
            bizId=biz_id,
            bizIdStr=str(biz_id),
            dynId=req.dynId,
            dynIdStr=str(req.dynId) if req.dynId is not None else None,
            isLike=is_like,
            likeCount=like_count,
        )
    )


@router.post(
    "/dislike",
    response_model=StandardResponse[MomentDislikeResp],
    summary="点踩 / 取消点踩（2.35.0）",
)
async def dislike(
    session: SessionDep,
    user: RequiredUser,
    req: MomentDislikeReq,
) -> StandardResponse[MomentDislikeResp]:
    """点踩 / 取消点踩（幂等）。

    MVP 仅支持动态资源：`bizType` 必须为 `dynamic`，`bizId` 与 `dynId` 任取其一。
    计数供 EdgeRank `dislike_ratio` 降权使用。
    """
    if req.bizType != InteractionBizTypeEnum.DYNAMIC:
        return StandardResponse(code=400, msg="点踩当前仅支持动态资源")
    biz_id = req.bizId if req.bizId is not None else req.dynId
    if biz_id is None:
        return StandardResponse(code=400, msg="bizId/dynId 不合法")
    try:
        action = DislikeAction(
            session,
            actor_mid=user.mid,
            biz_type=InteractionBizTypeEnum.DYNAMIC,
            biz_id=biz_id,
            up=req.up,
            dyn_id=biz_id,
        )
        is_dislike, dislike_count = await action.run()
    except ValueError as e:
        return StandardResponse(code=400, msg=str(e))
    return StandardResponse(
        data=MomentDislikeResp(
            bizType=InteractionBizTypeEnum.DYNAMIC,
            bizId=biz_id,
            bizIdStr=str(biz_id),
            dynId=biz_id,
            dynIdStr=str(biz_id),
            isDislike=is_dislike,
            dislikeCount=dislike_count,
        )
    )


@router.post(
    "/share",
    response_model=StandardResponse[MomentShareResp],
    summary="分享上报（2.35.0）",
)
async def share(
    session: SessionDep,
    user: RequiredUser,
    req: MomentShareReq,
) -> StandardResponse[MomentShareResp]:
    """分享上报：normal 动态 ``shareCount`` 原子 +1（行为上报，不幂等）。"""
    try:
        action = ShareAction(
            session,
            actor_mid=user.mid,
            biz_id=req.dynId,
            dyn_id=req.dynId,
        )
        count = await action.run()
    except ValueError as e:
        return StandardResponse(code=400, msg=str(e))
    return StandardResponse(
        data=MomentShareResp(
            dynId=req.dynId,
            dynIdStr=str(req.dynId),
            shareCount=count,
        )
    )


async def _verify_resources_exist(
    session: SessionDep, biz_type: InteractionBizTypeEnum, ids: list[int]
) -> list[str] | None:
    """批量校验资源存在性（2.23.1 防乱调）。

    - dynamic：本地查 TMoment（deletedAt 非空 / 非 normal 视为不存在）；
    - lottery：批量 RPC 校验（RPC 失败返回 None → 弱依赖降级放行，避免误伤正常用户）；
    - 其余未注册类型：放行。

    Returns:
        缺失的 bizId 字符串列表；全部存在返回 None。
    """
    if biz_type == InteractionBizTypeEnum.DYNAMIC:
        dyn_rows = (
            await session.exec(
                select(TMoment.dynId).where(
                    col(TMoment.dynId).in_(ids),
                    col(TMoment.deletedAt).is_(None),
                    col(TMoment.auditStatus) == MomentAuditStatusEnum.NORMAL,
                )
            )
        ).all()
        existing = {int(r) for r in dyn_rows}
    elif biz_type == InteractionBizTypeEnum.LOTTERY:
        from app.services.infrastructure.lottery_rpc import get_lottery_rpc_client

        client = await get_lottery_rpc_client()
        existing = await client.get_existing_lottery_ids(ids)
        if existing is None:
            existing = set(ids)  # RPC 校验不可用：降级放行
    else:
        existing = set(ids)
    missing = [str(_id) for _id in ids if _id not in existing]
    return missing or None


async def _query_status_items(
    session: SessionDep, biz_type: InteractionBizTypeEnum, ids: list[StrInt], mid: StrInt
) -> list[InteractionStatusItem]:
    """装配某类型多个资源的互动状态（计数 / 用户态 / 详情），供 status 两接口共用。"""
    # 2.36.0：动态与非动态资源计数统一 TInteractionStat（batch_get_counts 全字段）
    counts = await InteractionStatService.batch_get_counts(session, biz_type, ids)
    like_counts = {b: c["likeCount"] for b, c in counts.items()}
    fav_counts = {b: c["favoriteCount"] for b, c in counts.items()}
    view_counts = {b: c["viewCount"] for b, c in counts.items()}
    comment_counts = {b: c["commentCount"] for b, c in counts.items()}
    repost_counts = {b: c["repostCount"] for b, c in counts.items()}
    for _id in ids:
        like_counts.setdefault(_id, 0)
        fav_counts.setdefault(_id, 0)
    if not InteractionStatService.is_dynamic(biz_type):
        # 评论数：lottery 走评论系统实时计数（rpa_* 无评论功能 → 恒 0）
        if biz_type == InteractionBizTypeEnum.LOTTERY:
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
                    col(TMoment.bizType) == biz_type,
                    col(TMoment.bizRid).in_(ids),
                    col(TMoment.deletedAt).is_(None),
                    col(TMoment.auditStatus) == MomentAuditStatusEnum.NORMAL,
                )
                .group_by(TMoment.bizRid)
            )
        ).all()
        repost_counts = {int(r): int(c) for r, c in rows}

    # 当前用户点赞态 / 收藏态
    liked_ids = set(
        (
            await session.exec(
                select(TMomentLike.bizId).where(
                    col(TMomentLike.bizType) == biz_type,
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
                    col(TMomentFavorite.bizType) == biz_type,
                    col(TMomentFavorite.bizId).in_(ids),
                    col(TMomentFavorite.mid) == mid,
                )
            )
        ).all()
    )

    # 非动态资源（RPA 等）详情经 RPC 从资源归属服务获取（弱依赖，失败 detail=None）
    details: dict[int, object] = {}
    if not InteractionStatService.is_dynamic(biz_type):
        results = await asyncio.gather(
            *[rpa_rpc_client.get_resource_detail(biz_type.to_text(), _id) for _id in ids],
            return_exceptions=True,
        )
        for _id, res in zip(ids, results):
            detail = None
            if isinstance(res, Exception):
                detail = None
            elif res is not None and getattr(res, "detail", None) is not None:
                detail = res.detail
            details[_id] = detail

    # 2.40.0：被举报人数（去重举报人，同一人多次举报只记一次）
    # dynamic → bizType='dynamic'；非动态 → resourceType=资源类型枚举值
    report_counts: dict[int, int] = {}
    if ids:
        if InteractionStatService.is_dynamic(biz_type):
            _rp_where = (
                col(TResourceReport.bizType) == ReportBizTypeEnum.DYNAMIC.value,
                col(TResourceReport.bizId).in_(ids),
            )
        else:
            _rp_where = (
                col(TResourceReport.resourceType) == int(biz_type),
                col(TResourceReport.bizId).in_(ids),
            )
        rp_rows = (
            await session.exec(
                select(
                    TResourceReport.bizId,
                    func.count(),
                    func.count(func.distinct(TResourceReport.reportMid)),
                )
                .where(*_rp_where)
                .group_by(col(TResourceReport.bizId))
            )
        ).all()
        report_counts = {int(b): (int(c), int(p)) for b, c, p in rp_rows}

    return [
        InteractionStatusItem(
            bizType=biz_type,
            bizId=str(_id),
            isLike=_id in liked_ids,
            isFavorite=_id in faved_ids,
            likeCount=like_counts.get(_id, 0),
            favoriteCount=fav_counts.get(_id, 0),
            commentCount=comment_counts.get(_id, 0),
            repostCount=repost_counts.get(_id, 0),
            viewCount=view_counts.get(_id, 0),
            reportCount=report_counts.get(_id, (0, 0))[0],
            reportPeopleCount=report_counts.get(_id, (0, 0))[1],
            detail=details.get(_id),
        )
        for _id in ids
    ]


@router.get(
    "/interaction/status",
    response_model=StandardResponse[InteractionStatusResp],
    summary="批量查询某类型资源当前用户收藏/点赞态 + 计数（列表专用，不累计浏览）",
)
async def interaction_status(
    session: SessionDep,
    user: RequiredUser,
    bizType: InteractionBizTypeEnum = Query(description="资源类型（InteractionBizTypeEnum 值）"),
    bizIds: str = Query(description="资源 id 列表（逗号分隔，限 50 个）"),
) -> StandardResponse[InteractionStatusResp]:
    try:
        ids = [int(x.strip()) for x in bizIds.split(",") if x.strip()]
    except ValueError:
        return StandardResponse(code=400, msg="bizIds 不合法")
    if not ids:
        return StandardResponse(code=400, msg="bizIds 不能为空")
    ids = ids[:50]
    mid = user.mid

    # 防乱调（2.23.1）：任一缺失 → 整体 400，不返回部分结果（列表接口不投递浏览 MQ）
    missing = await _verify_resources_exist(session, bizType, ids)
    if missing:
        return StandardResponse(code=400, msg=f"资源不存在: {', '.join(missing)}")

    items = await _query_status_items(session, bizType, ids, mid)
    return StandardResponse(data=InteractionStatusResp(items=items))


@router.get(
    "/interaction/status/{biz_id}",
    response_model=StandardResponse[InteractionStatusItem],
    summary="单资源互动状态（detail 页专用，兼作浏览统计触发点）",
)
async def interaction_status_detail(
    session: SessionDep,
    user: RequiredUser,
    biz_id: str,
    bizType: InteractionBizTypeEnum = Query(description="资源类型（InteractionBizTypeEnum 值）"),
) -> StandardResponse[InteractionStatusItem]:
    """查询单个资源互动状态；detail 页调用，查询后投递浏览 MQ 异步累计（2.23.1）。

    列表批量接口不累计浏览，仅进入详情页（本接口）才 +1——
    经 ViewLog 按 bizType+bizId+mid+refDate 去重幂等，同日重复进入详情不重复计数。
    """
    biz_type = bizType
    try:
        biz_id_int = int(biz_id)
    except ValueError:
        return StandardResponse(code=400, msg="bizId 不合法")

    # 防乱调：资源不存在 → 400（不投递浏览）
    missing = await _verify_resources_exist(session, biz_type, [biz_id_int])
    if missing:
        return StandardResponse(code=400, msg=f"资源不存在: {', '.join(missing)}")

    item = (await _query_status_items(session, biz_type, [biz_id_int], user.mid))[0]

    # 浏览统计触发点（detail 专用）：投递 MQ 异步去重累计，主链路不阻塞
    # 2.42.0：不再携带 refDate——消费端按 (bizType,bizId,mid) 每用户每资源一行，
    # 由 lastViewAt 是否同一自然日判断跨天访问才 +1
    await publish_interaction_view(
        InteractionViewPayload(
            bizType=biz_type, bizId=str(biz_id_int), mid=user.mid
        )
    )
    return StandardResponse(data=item)


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
        action = ReportAction(
            session,
            actor_mid=user.mid,
            biz_id=req.dynId,
            reason_type=req.reasonType,
            reason_desc=req.reasonDesc,
            dyn_id=req.dynId,
        )
        await action.run()
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
    topicId: StrInt,
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
    topicId: StrInt,
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


# 空间统计（对标 B 站 upstat）原 `GET /upstat` 端点已于 2.52.0 删除：
# 统计随 `GET /user/space/info` 的 `upstat` 字段一次返回（见 api/pptr_user_gateway.py）。
# 服务层 `MomentFeedService.get_upstat` 由该路由复用，保留。
