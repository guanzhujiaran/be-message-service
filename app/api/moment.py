"""动态发布 / 互动 / 话题&@&POI 类 HTTP 接口（/api/v1/community）。

覆盖 P2-T7（发布）+ P4-T7（互动）+ P5-T7（话题 / @ / POI）：

发布类：
- POST /create        创建动态（WORD / FORWARD），响应 auditStatus='auditing'；WORD 受每日上限
- POST /remove        删除动态（软删，仅作者本人）
- POST /admin/remove  管理员删除动态（软删，2.22.1，仅 root，可删任意动态）
- POST /repost        转发动态（FORWARD）
- POST /space/top     空间置顶（仅本人 + normal）
- POST /space/untop   取消置顶
- POST /create/check  发布页预校验

互动类（P4-T7，2.48.0 起统一由 `interaction_actions.BaseBiz` 资源类承载，按 bizType 分发；
2.56.0 起四个写接口全面通用化，一律以 `bizType`+`bizId` 定位资源（去除 dynId 别名）：
- POST /thumb         点赞 / 取消点赞（幂等，BaseBiz.like）
- POST /dislike       点踩 / 取消点踩（幂等，BaseBiz.dislike）
- POST /share         分享上报（shareCount +1，BaseBiz.share）
- POST /report        举报资源（不改 auditStatus，BaseBiz.report）
- GET  /interaction/status[/{biz_id}]  互动态查询（批量 / 单资源，兼作浏览统计触发点；
  2.60.0 起匿名可读 OptionalUser，浏览统计仍仅登录用户）
（浏览计数无上报接口：由后端在详情接口 GET /detail/{id} 访问时自动累计）

分层约定（2.56.0，计划书 §5.13 / C21）：参数归一 `resolve_target()`、存在性校验与互动态
装配在 `app.services.interaction_actions.interaction_status.InteractionStatusService`，
本路由层只做「参数归一 → get_biz(...).动作() → 装配响应模型」，不含查询编排与 bizType 分流。

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

from fastapi import APIRouter, Header, Query, Request

from app.models.str_int import StrInt

from app.core.database import SessionDep
from app.dependencies import RequiredUser, OptionalUser, RootUser
from app.models import StandardResponse
from bili_common.models import InteractionBizTypeEnum
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
from app.services.message.infrastructure.publisher import publish_interaction_view
from app.services.interaction_actions import get_biz
from app.services.interaction_actions.interaction_status import (
    InteractionStatusService,
    resolve_target,
)
from app.services.moment.moment_feed import MomentFeedService
from app.services.moment.topic_feed import TopicFeedService
from app.services.moment.moment_publish import (
    MomentDailyCreateLimitError,
    MomentPublishService,
)
from app.services.moment.moment_topic import (
    MomentTopicDailyCreateLimitError,
    MomentTopicService,
)
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
    except MomentDailyCreateLimitError as e:
        return StandardResponse(code=int(e.code), msg=str(e), data=None)
    except ValueError as e:
        return StandardResponse(code=400, msg=str(e))
    return StandardResponse(data=MomentCreateResp(**data))


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
        # 转发是动态专属语义（生成 FORWARD 动态），固定按 dynamic 取资源实例
        biz = get_biz(
            InteractionBizTypeEnum.DYNAMIC, session, int(req.srcDynId), user.mid
        )
        biz.client_ip = ip
        biz.user_agent = ua
        data = await biz.repost(content=req.content)
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


# ==================== 互动类（P4-T7；2.56.0 全面通用化，计划书 §5.13）====================
# 四个互动写接口一律以 `bizType` + `bizId` 唯一定位资源（2.56.0 去除 dynId 别名）；
# 经 `resolve_target()` 归一后由 `get_biz()` 取资源实例调用对应方法；
# 存在性校验与互动态装配在 `InteractionStatusService`，路由层只做三步：
# 参数归一 → get_biz(...).动作() → 装配响应模型。
#
# 动作语义：
# - /thumb   点赞 / 取消点赞（幂等，BaseBiz.like）
# - /dislike 点踩 / 取消点踩（幂等，BaseBiz.dislike）
# - /share   分享上报（shareCount +1，不幂等，BaseBiz.share）
# - /report  举报资源（不改 auditStatus，BaseBiz.report）


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
    """点赞 / 取消点赞（幂等）。`bizType` + `bizId` 定位任意资源。"""
    try:
        biz_type, biz_id = resolve_target(req.bizType, req.bizId)
        biz = get_biz(biz_type, session, biz_id, user.mid)
        is_like, like_count = await biz.like(up=req.up)
    except ValueError as e:
        return StandardResponse(code=400, msg=str(e))
    return StandardResponse(
        data=MomentThumbResp(
            bizType=biz_type,
            bizId=biz_id,
            bizIdStr=str(biz_id),
            isLike=is_like,
            likeCount=like_count,
        )
    )


@router.post(
    "/dislike",
    response_model=StandardResponse[MomentDislikeResp],
    summary="点踩 / 取消点踩（2.35.0；2.56.0 支持全部资源）",
)
async def dislike(
    session: SessionDep,
    user: RequiredUser,
    req: MomentDislikeReq,
) -> StandardResponse[MomentDislikeResp]:
    """点踩 / 取消点踩（幂等）。`bizType` + `bizId` 定位任意资源。

    计数供 EdgeRank `dislike_ratio` 降权使用。
    """
    try:
        biz_type, biz_id = resolve_target(req.bizType, req.bizId)
        biz = get_biz(biz_type, session, biz_id, user.mid)
        is_dislike, dislike_count = await biz.dislike(up=req.up)
    except ValueError as e:
        return StandardResponse(code=400, msg=str(e))
    return StandardResponse(
        data=MomentDislikeResp(
            bizType=biz_type,
            bizId=biz_id,
            bizIdStr=str(biz_id),
            isDislike=is_dislike,
            dislikeCount=dislike_count,
        )
    )


@router.post(
    "/share",
    response_model=StandardResponse[MomentShareResp],
    summary="分享上报（2.35.0；2.56.0 支持全部资源）",
)
async def share(
    session: SessionDep,
    user: RequiredUser,
    req: MomentShareReq,
) -> StandardResponse[MomentShareResp]:
    """分享上报：``shareCount`` 原子 +1（行为上报，不幂等）。

    `bizType` + `bizId` 定位任意资源（缺省 dynamic）。
    """
    try:
        biz_type, biz_id = resolve_target(req.bizType, req.bizId)
        biz = get_biz(biz_type, session, biz_id, user.mid)
        count = await biz.share()
    except ValueError as e:
        return StandardResponse(code=400, msg=str(e))
    return StandardResponse(
        data=MomentShareResp(
            bizType=biz_type,
            bizId=biz_id,
            bizIdStr=str(biz_id),
            shareCount=count,
        )
    )


@router.get(
    "/interaction/status",
    response_model=StandardResponse[InteractionStatusResp],
    summary="批量查询某类型资源当前用户收藏/点赞态 + 计数（列表专用，不累计浏览）",
)
async def interaction_status(
    session: SessionDep,
    user: OptionalUser,
    bizType: InteractionBizTypeEnum = Query(description="资源类型（InteractionBizTypeEnum 值）"),
    bizIds: str = Query(description="资源 id 列表（逗号分隔，限 50 个）"),
) -> StandardResponse[InteractionStatusResp]:
    """批量互动态（2.60.0 起匿名可读，计划书 §5.18）。

    匿名时 `user` 为 None → `viewer_mid=0`（非合法 mid），点赞 / 收藏态恒 false，
    计数与举报数照常返回。
    """
    # bizIds 为逗号分隔的雪花 ID 字符串，含非数字片段时 int() 抛 ValueError：
    # 必须兜住并转 400 错误响应（业务码 400 = INVALID_PARAM），否则会穿透到全局
    # 兜底处理器变成 500。
    try:
        ids = [int(x.strip()) for x in bizIds.split(",") if x.strip()]
    except ValueError:
        return StandardResponse(code=400, msg="bizIds 不合法")
    if not ids:
        return StandardResponse(code=400, msg="bizIds 不能为空")
    ids = ids[:50]

    # 防乱调（2.23.1）：任一缺失 → 整体 400，不返回部分结果（列表接口不投递浏览 MQ）
    missing = await InteractionStatusService.verify_resources_exist(
        session, bizType, ids
    )
    if missing:
        return StandardResponse(code=400, msg=f"资源不存在: {', '.join(missing)}")

    items = await InteractionStatusService.query_status_items(
        session, bizType, ids, user.mid if user else 0
    )
    return StandardResponse(data=InteractionStatusResp(items=items))


@router.get(
    "/interaction/status/{biz_id}",
    response_model=StandardResponse[InteractionStatusItem],
    summary="单资源互动状态（detail 页专用，兼作浏览统计触发点）",
)
async def interaction_status_detail(
    session: SessionDep,
    user: OptionalUser,
    biz_id: str,
    bizType: InteractionBizTypeEnum = Query(description="资源类型（InteractionBizTypeEnum 值）"),
) -> StandardResponse[InteractionStatusItem]:
    """查询单个资源互动状态；detail 页调用，查询后投递浏览 MQ 异步累计（2.23.1）。

    列表批量接口不累计浏览，仅进入详情页（本接口）才 +1——
    经 ViewLog 按 bizType+bizId+mid+refDate 去重幂等，同日重复进入详情不重复计数。

    2.60.0（§5.18）：匿名可读，`user` 为 None 时 `viewer_mid=0`（点赞 / 收藏态恒 false）；
    浏览 MQ **仅登录用户投递**——匿名无 mid，`TInteractionViewLog`（uq bizType+bizId+mid）
    会把全部游客流量压成 mid=0 一行，计数失真且污染明细表，沿用「浏览统计仅登录用户」语义。
    """
    biz_type = bizType
    try:
        biz_id_int = int(biz_id)
    except ValueError:
        return StandardResponse(code=400, msg="bizId 不合法")

    # 防乱调：资源不存在 → 400（不投递浏览）
    missing = await InteractionStatusService.verify_resources_exist(
        session, biz_type, [biz_id_int]
    )
    if missing:
        return StandardResponse(code=400, msg=f"资源不存在: {', '.join(missing)}")

    item = await InteractionStatusService.query_status_item(
        session, biz_type, biz_id_int, user.mid if user else 0
    )
    if item is None:
        return StandardResponse(code=400, msg="资源不存在")

    # 浏览统计触发点（detail 专用）：投递 MQ 异步去重累计，主链路不阻塞
    # 2.42.0：不再携带 refDate——消费端按 (bizType,bizId,mid) 每用户每资源一行，
    # 由 lastViewAt 是否同一自然日判断跨天访问才 +1
    # 2.60.0：匿名（user 为 None）不投递——无 mid 无法归属，见函数 docstring
    if user:
        await publish_interaction_view(
            InteractionViewPayload(
                bizType=biz_type, bizId=str(biz_id_int), mid=user.mid
            )
        )
    return StandardResponse(data=item)


@router.post(
    "/report",
    response_model=StandardResponse[MomentReportResp],
    summary="举报资源（2.56.0 支持全部资源）",
)
async def report(
    session: SessionDep,
    user: RequiredUser,
    req: MomentReportReq,
) -> StandardResponse[MomentReportResp]:
    """举报资源（幂等，不改资源状态，达阈值仅加入审核队列）。

    `bizType` + `bizId` 定位任意资源（缺省 dynamic）；
    统一举报另有独立域 `POST /api/v1/report`，两者等价。
    """
    try:
        biz_type, biz_id = resolve_target(req.bizType, req.bizId)
        biz = get_biz(biz_type, session, biz_id, user.mid)
        await biz.report(reason_type=req.reasonType, reason_desc=req.reasonDesc)
    except ValueError as e:
        return StandardResponse(code=400, msg=str(e))
    return StandardResponse(
        data=MomentReportResp(
            bizType=biz_type,
            bizId=biz_id,
            bizIdStr=str(biz_id),
        )
    )


# ==================== 话题 & @ & POI（P5-T7）====================

_SHOWLIST_LIMIT = 100


def _parse_int_list(raw: str | None) -> list[int] | None:
    """解析逗号分隔的正整数列表（topicId / dynId 通用）。

    与 moment_feed._parse_dyn_id_list 同语义：去重、过滤非正整数、上限 100，
    空则返回 None（服务端不做去重）。本文件不反向依赖 moment_feed 以免循环导入。
    """
    if not raw:
        return None
    seen: set[int] = set()
    result: list[int] = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            v = int(part)
        except (TypeError, ValueError):
            continue
        if v > 0 and v not in seen:
            seen.add(v)
            result.append(v)
            if len(result) >= _SHOWLIST_LIMIT:
                break
    return result or None


@router.get(
    "/topic/square",
    response_model=StandardResponse[MomentTopicSquareResp],
    summary="话题广场（推荐流）",
)
async def topic_square(
    session: SessionDep,
    user: OptionalUser,
    page_size: int = Query(20, ge=1, le=50, description="单页条数（推荐流，对齐 feed ps）"),
    last_showlist: str | None = Query(None, description="已展示的 topicId 列表（逗号分隔，服务端去重，上限 100）"),
    keyword: str | None = Query(None, description="话题名关键词搜索（模糊匹配 topicName）"),
    hot_only: bool = Query(False, description="仅返回热门话题（isHot=1），对齐 /topic/hot-search"),
) -> StandardResponse[MomentTopicSquareResp]:
    data = await MomentTopicService.topic_square(
        session,
        page_size=page_size,
        last_showlist=_parse_int_list(last_showlist),
        keyword=keyword,
        hot_only=hot_only,
    )
    return StandardResponse(data=data)


@router.get(
    "/topic/hot-search",
    response_model=StandardResponse[MomentTopicSquareResp],
    summary="热门话题",
)
async def topic_hot_search(
    session: SessionDep,
    user: OptionalUser,
    page: int = 1,  # 兼容旧调用（发布表单传 page=1），推荐流忽略
    page_size: int = Query(20, ge=1, le=50),
    last_showlist: str | None = Query(None, description="已展示 topicId 列表（逗号分隔，去重）"),
) -> StandardResponse[MomentTopicSquareResp]:
    data = await MomentTopicService.topic_square(
        session, page_size=page_size, last_showlist=_parse_int_list(last_showlist), hot_only=True
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
        session, topic_id=int(topicId), viewer_mid=user.mid
    )
    if data is None:
        return StandardResponse(code=404, msg="话题不存在")
    return StandardResponse(data=data)


@router.get(
    "/topic/feed/{topicId}",
    response_model=StandardResponse[MomentTopicFeedResp],
    response_model_exclude_none=True,
    summary="话题动态流（recommend 推荐流默认 / time 最新；hot 为 recommend 兼容别名）",
)
async def topic_feed(
    session: SessionDep,
    user: RequiredUser,
    topicId: StrInt,
    sort: str = Query(
        "recommend",
        description="排序：recommend=EdgeRank 推荐流（默认，last_showlist 去重）/ time=最新（pubTime 倒序 + historyOffset 游标）/ hot=recommend 兼容别名",
    ),
    page: int = 1,
    page_size: int = 20,
    history_offset: int | None = None,
    last_showlist: str | None = Query(
        None, description="已展示的 dynId 列表（逗号分隔，recommend 去重，上限 100）"
    ),
    last_clicklist: str | None = Query(
        None, description="已互动的 dynId 列表（逗号分隔，个性化反馈预留，当前不参与排序）"
    ),
    uniq_id: str | None = Query(None, description="客户端唯一 ID（匿名随机排序种子）"),
) -> StandardResponse[MomentTopicFeedResp]:
    data = await TopicFeedService.topic_feed(
        session,
        topic_id=int(topicId),
        page=page,
        page_size=page_size,
        viewer_mid=user.mid,
        history_offset=history_offset,
        sort=sort if sort in ("recommend", "time", "hot") else "recommend",
        last_showlist=_parse_int_list(last_showlist),
        last_clicklist=_parse_int_list(last_clicklist),
        uniq_id=uniq_id,
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
    except MomentTopicDailyCreateLimitError as e:
        return StandardResponse(code=int(e.code), msg=str(e), data=None)
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
