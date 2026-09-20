"""评论系统 HTTP 接口（/api/v1/comment）。

对应 B 站评论系统架构中的 `reply-interface` 对外 REST 层，覆盖 Phase 1 的基础读写：

- `POST /add`    发表评论（一级 / 楼中楼），登录态；同事务落索引 + 正文 + 计数。
  2.63.0 起经资源类 `get_biz(...).reply()` 分发（一级 → 目标资源；楼中楼 → 根评论），
  「类型是否支持评论」「资源是否存在 / 可互动」由资源类与 `@biz_action` 承担，
  本路由不含类型白名单。
- `POST /del`    删除评论（作者 / UP 主 / 管理员），登录态。
- `GET  /main`   一级评论列表（含置顶），读冗余计数，4 次常量 SQL 组装。
- `GET  /detail` 单条评论详情。
- `GET  /count`  评论区计数（root_count / all_count）。

列表 / 详情 / 计数对未登录用户同样可读（viewer_mid 缺失时仅不展示「我的点赞态」）。
IP 一律按 D3 约定：服务端只存原始 v4/v6，出参在 read 服务层打码，本路由不处理展示。
"""

from fastapi import APIRouter, Depends, Header, Query, Request

from app.core.database import SessionDep
from app.dependencies import RequiredUser
from app.models import StandardResponse
from bili_common.models import InteractionBizTypeEnum
from app.models.enums import BanServiceEnum, CommentSortEnum
from app.models.schemas import (
    CommentActionReq,
    CommentActionResp,
    CommentAddReq,
    CommentAddResp,
    CommentCountResp,
    CommentDelReq,
    CommentItem,
    CommentLatestResp,
    CommentListResp,
    CommentOperationResp,
    CommentReportReq,
    CommentReportResp,
    CommentSubListResp,
    CommentTopReq,
    CommentTopResp,
    UserBriefOut,
    )
from app.services.comment import CommentService
from app.services.comment.comment_action import CommentActionService
from app.services.comment.comment_read import CommentReadService
from app.services.comment.comment import CommentDailyCreateLimitError
from app.services.interaction_actions import get_biz
from app.services.user.account import CommentAdminUser
from app.utils.ip_mask import extract_client_ip

router = APIRouter(prefix="/api/v1/comment", tags=["comment"])


def _to_positive_int(raw: str | int | None) -> int | None:
    """字符串 ID → 正整数；空 / 非数字 / 非正一律返回 None（与评论服务的归一口径一致）。"""
    if raw is None:
        return None
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def resolve_ip_geo_pairs(
    ip_v4: str | None, ip_v6: str | None
) -> tuple[str | None, str | None]:
    """按客户端 IP 解析属地 + ISP（GeoIP，失败静默降级）。

    优先 IPv4（更接近真实接入点），无 v4 时回落 v6；两者都没有返回 None。
    返回 `(ip_location, ip_isp)`，供评论/动态发布时保存展示。
    """
    try:
        from app.services.infrastructure.geo_ip import lookup

        ip = ip_v4 or ip_v6
        r = lookup(ip)
        # lookup 内部已用「未知」兜底（不抛异常），这里防御性保留 None 判断
        if r is None:
            return None, None
        return r.poi, r.isp
    except Exception:  # noqa: BLE001 - 解析失败静默降级
        return None, None


# ==================== 可选登录态（未登录则 viewer_mid=None）====================


async def resolve_optional_viewer(
    x_bili_mid: str | None = Header(default=None),
) -> int | None:
    """解析可选登录态，仅取 mid。

    列表 / 详情 / 计数允许匿名访问：未携带 x-bili-mid（或非法）时回落 None，
    仅意味着「不展示当前用户的点赞态」，不影响评论数据的读取。
    """
    if not x_bili_mid:
        return None
    try:
        mid = int(x_bili_mid)
    except (TypeError, ValueError):
        return None
    return mid if mid > 0 else None


# ==================== 发布 / 删除 ====================


@router.post("/add", response_model=StandardResponse[CommentAddResp], summary="发表评论")
async def add_comment(
    session: SessionDep,
    user: RequiredUser,
    req: CommentAddReq,
    request: Request,
    user_agent: str | None = Header(default=None, alias="user-agent"),
) -> StandardResponse[CommentAddResp]:
    """发表一条评论（一级或楼中楼）。

    通过 x-bili-* 头识别登录用户；作者展示信息（昵称等）由列表接口按需从 pptr
    Postgres 只读取回，本服务不再冗余用户快照。客户端真实 IP 从网关注入的头里提取，
    仅存原始地址。

    **资源为主体（2.63.0，计划书 §5.9）**：统一经 `get_biz(...).reply()` 分发——
    一级评论目标是资源本身（`dynamic` / `lottery` / `others_lot_dyn` / `rpa_*`…），
    楼中楼目标是根评论（`CommentBiz`，其内部按评论所属评论区定位 `oid`/`type`）。
    「该类型是否支持评论」由资源类表达能力决定（未实现 `reply` 的类型直接报错），
    「资源是否存在 / 是否可互动」由 `@biz_action` 校验（写路径严格、不降级），
    因此本路由**不需要也不允许**维护可评论类型白名单。
    """
    # 封禁校验：被封禁「评论」服务的用户禁止发表评论
    if await CommentAdminUser(mid=user.mid).is_banned(session, BanServiceEnum.COMMENT.value):
        return StandardResponse(code=403, msg="该账号已被封禁评论功能，无法发表评论")

    ip_v4, ip_v6 = extract_client_ip(
        dict(request.headers),
        request.client.host if request.client else None,
    )
    # 服务端按客户端 IP 解析属地 + ISP（GeoIP，失败静默降级为 None）
    ip_location, ip_isp = resolve_ip_geo_pairs(ip_v4, ip_v6)

    # 参数归一：oid 必填；root / parent 为 rpid 字符串，"0" / 缺省 / 非法一律视为 0
    oid = _to_positive_int(req.oid)
    if oid is None:
        return StandardResponse(code=400, msg="oid 不合法")
    root = _to_positive_int(req.root) or 0
    parent = _to_positive_int(req.parent) or 0

    try:
        biz = get_biz(
            InteractionBizTypeEnum.COMMENT if root else req.type,
            session,
            root or oid,
            user.mid,
        )
        # 客户端上下文注入资源实例：评论正文要落 IP / 属地，回复与 @ 通知要带昵称
        biz.actor_uname = user.uname
        biz.client_ip_v4 = ip_v4
        biz.client_ip_v6 = ip_v6
        biz.user_agent = user_agent
        biz.ip_location = ip_location
        biz.ip_isp = ip_isp
        data = await biz.reply(
            req.message,
            parent=parent,
            at_mids=req.at_mids,
            at_name_to_mid=req.at_name_to_mid,
            pictures=req.pictures,
            emote_meta=req.emote_meta,
            up_mid=req.up_mid or 0,
        )
    except CommentDailyCreateLimitError as e:
        return StandardResponse(code=int(e.code), msg=str(e), data=None)
    except NotImplementedError as e:
        # 该资源类型未实现 reply（审核域 / 用户等不支持评论的目标）：400，不落到 500
        return StandardResponse(code=400, msg=str(e))
    except ValueError as e:
        return StandardResponse(code=400, msg=str(e))
    return StandardResponse(data=data)


@router.post("/del", response_model=StandardResponse[CommentOperationResp], summary="删除评论")
async def delete_comment(
    session: SessionDep,
    user: RequiredUser,
    req: CommentDelReq,
) -> StandardResponse[CommentOperationResp]:
    """删除评论（软删）。

    权限：评论作者本人 / 内容作者（UP 主）/ 管理员。其余角色返回无权提示。
    """
    try:
        rpid = int(str(req.rpid).strip())
        if rpid <= 0:
            raise ValueError("rpid 不合法")
    except (TypeError, ValueError):
        return StandardResponse(code=400, msg="rpid 不合法")

    is_admin = user.role == "root"
    affected, message = await CommentService.delete(
        session, user.mid, rpid, is_admin=is_admin
    )
    return StandardResponse(
        data=CommentOperationResp(affected=affected, message=message)
    )


# ==================== 列表 / 详情 / 计数 ====================


@router.get("/main", response_model=StandardResponse[CommentListResp], summary="一级评论列表")
async def list_main(
    session: SessionDep,
    _viewer: int | None = Depends(resolve_optional_viewer),
    oid: str = Query(description="业务实体id（字符串，雪花ID）"),
    type: InteractionBizTypeEnum = Query(description="业务实体类型"),
    sort: CommentSortEnum = Query(default=CommentSortEnum.HOT, description="排序：hot 热度 / time 时间"),
    page_num: int = Query(default=1, ge=1, description="页码，从 1 开始"),
    page_size: int = Query(default=20, ge=1, le=50, description="每页条数"),
    focus_rpid: str | None = Query(default=None, description="定位评论rpid：该评论（或其根评论）会被提到列表顶部并回填 focus 字段，用于通知/外链直达"),
) -> StandardResponse[CommentListResp]:
    """一级评论列表（含置顶评论）。

    置顶评论不参与分页，始终单独返回并置于列表顶部；总数读评论区冗余计数，
    不在本接口做 COUNT(*)。SQL 次数恒定（主列表 + 置顶 + 4 次批量回捞）。
    """
    try:
        oid_int = int(oid)
        if oid_int <= 0:
            raise ValueError("oid 不合法")
    except (TypeError, ValueError):
        return StandardResponse(code=400, msg="oid 不合法")

    focus_int = None
    if focus_rpid:
        try:
            focus_int = int(focus_rpid)
            if focus_int <= 0:
                raise ValueError
        except (TypeError, ValueError):
            return StandardResponse(code=400, msg="focus_rpid 不合法")

    # 未登录（viewer_mid 缺失）：对标 B 站强制最多 10 条评论（不信任前端传入的 page_size），
    # 并在响应里透传 viewer_is_anonymous 供前端渲染登录引导蒙层
    is_anonymous = _viewer is None
    if is_anonymous:
        page_size = 10

    data = await CommentReadService.list_main(
        session,
        oid_int,
        type,
        sort=sort,
        page_num=page_num,
        page_size=page_size,
        viewer_mid=_viewer,
        focus_rpid=focus_int,
        viewer_is_anonymous=is_anonymous,
    )
    return StandardResponse(data=data)


@router.get("/detail/{rpid}", response_model=StandardResponse[CommentItem], summary="评论详情")
async def comment_detail(
    session: SessionDep,
    rpid: str,
    _viewer: int | None = Depends(resolve_optional_viewer),
) -> StandardResponse[CommentItem]:
    """单条评论详情。已删除 / 已下架的评论返回 404。"""
    try:
        rpid_int = int(rpid)
        if rpid_int <= 0:
            raise ValueError("rpid 不合法")
    except (TypeError, ValueError):
        return StandardResponse(code=400, msg="rpid 不合法")

    item = await CommentReadService.get_detail(session, rpid_int, viewer_mid=_viewer)
    if item is None:
        return StandardResponse(code=404, msg="评论不存在或已删除")
    return StandardResponse(data=item)


@router.get("/count", response_model=StandardResponse[CommentCountResp], summary="评论区计数")
async def comment_count(
    session: SessionDep,
    oid: str = Query(description="业务实体id（字符串）"),
    type: InteractionBizTypeEnum = Query(description="业务实体类型"),
) -> StandardResponse[CommentCountResp]:
    """评论区计数（root_count / all_count）。

    评论区尚未开区时返回全 0（不报错），前端可据此直接展示「还没有评论」。
    """
    try:
        oid_int = int(oid)
        if oid_int <= 0:
            raise ValueError("oid 不合法")
    except (TypeError, ValueError):
        return StandardResponse(code=400, msg="oid 不合法")

    data = await CommentReadService.get_count(session, oid_int, type)
    return StandardResponse(data=data)


@router.get("/latest", response_model=StandardResponse[CommentLatestResp], summary="首页最新评论（按资源类型分组）")
async def comment_latest(
    session: SessionDep,
    _viewer: int | None = Depends(resolve_optional_viewer),
    types: str | None = Query(
        default=None,
        description="逗号分隔的资源类型文字（dynamic/lottery/rpa_action/rpa_workflow/rpa_browser/rpa_plugin），缺省返回全部可挂评论的类型",
    ),
    limit: int = Query(default=5, ge=1, le=20, description="每个资源类型取最新根评论条数"),
) -> StandardResponse[CommentLatestResp]:
    """首页「最新评论」：按资源类型分组，仅返回各类型最新 N 条根评论。

    - 只展示根评论，楼中楼子评论不返回；
    - 未登录 / 登录均可访问，`x-bili-mid` 存在时仅影响「我的点赞态」回填。
    """
    type_list: list[InteractionBizTypeEnum] | None = None
    if types:
        cleaned = [t.strip().lower() for t in types.split(",") if t.strip()]
        if not cleaned:
            return StandardResponse(code=400, msg="types 参数不合法")
        try:
            type_list = [InteractionBizTypeEnum.from_text(t) for t in cleaned]
        except (ValueError, KeyError):
            return StandardResponse(code=400, msg="types 参数不合法")

    data = await CommentReadService.list_latest(
        session,
        types=type_list,
        limit_per_type=limit,
        viewer_mid=_viewer,
    )
    return StandardResponse(data=data)


# ==================== 楼中楼 / 互动 / @ / 置顶 ====================


@router.get("/reply", response_model=StandardResponse[CommentSubListResp], summary="楼中楼展开")
async def reply_list(
    session: SessionDep,
    _viewer: int | None = Depends(resolve_optional_viewer),
    root: str = Query(description="根评论rpid（字符串）"),
    oid: str = Query(description="业务实体id（字符串）"),
    type: InteractionBizTypeEnum = Query(description="业务实体类型"),
    page_num: int = Query(default=1, ge=1, description="页码，从 1 开始"),
    page_size: int = Query(default=20, ge=1, le=50, description="每页条数"),
) -> StandardResponse[CommentSubListResp]:
    """楼中楼（子评论）分页展开，用于「共 N 条回复」的加载更多。

    `total` 读根评论冗余 `rcount`，不 `COUNT(*)`；按 rpid 顺序即发布顺序。
    """
    try:
        root_int = int(root)
        if root_int <= 0:
            raise ValueError("root 不合法")
    except (TypeError, ValueError):
        return StandardResponse(code=400, msg="root 不合法")
    try:
        oid_int = int(oid)
        if oid_int <= 0:
            raise ValueError("oid 不合法")
    except (TypeError, ValueError):
        return StandardResponse(code=400, msg="oid 不合法")

    data = await CommentReadService.get_sub_list(
        session,
        root_int,
        oid_int,
        type,
        page_num=page_num,
        page_size=page_size,
        viewer_mid=_viewer,
    )
    return StandardResponse(data=data)


@router.post("/action", response_model=StandardResponse[CommentActionResp], summary="点赞/点踩/取消")
async def comment_action(
    session: SessionDep,
    user: RequiredUser,
    req: CommentActionReq,
) -> StandardResponse[CommentActionResp]:
    """对一条评论点赞 / 点踩 / 取消（NONE）。

    幂等：重复点赞不会重复计数；赞 → 踩 → 取消的状态翻转在同一事务内修正计数。
    被点赞会通过事件服务弱依赖地通知评论作者。
    """
    try:
        rpid = int(str(req.rpid).strip())
        if rpid <= 0:
            raise ValueError("rpid 不合法")
    except (TypeError, ValueError):
        return StandardResponse(code=400, msg="rpid 不合法")

    try:
        data = await CommentActionService.action(session, user.mid, rpid, req.action)
    except ValueError as e:
        return StandardResponse(code=400, msg=str(e))
    return StandardResponse(data=data)


@router.post("/report", response_model=StandardResponse[CommentReportResp], summary="举报评论")
async def report_comment(
    session: SessionDep,
    user: RequiredUser,
    req: CommentReportReq,
) -> StandardResponse[CommentReportResp]:
    """举报评论（幂等：一人对同一评论只记一次）。

    累计有效举报数达 `settings.comment_report_threshold`（默认 3）时，
    评论 state 由 `normal` → `auditing`，交管理员复核。
    """
    try:
        rpid = int(req.rpid)
    except (TypeError, ValueError):
        return StandardResponse(code=400, msg="rpid 不合法")
    try:
        reported, switched = await CommentService.report(
            session,
            user.mid,
            rpid,
            req.reasonType,
            req.reasonDesc,
        )
    except ValueError as e:
        return StandardResponse(code=400, msg=str(e))
    return StandardResponse(
        data=CommentReportResp(
            rpid=req.rpid,
            reported=reported,
            switched_to_auditing=switched,
        )
    )


@router.get("/at/search", response_model=StandardResponse[list[UserBriefOut]], summary="@用户搜索")
async def at_search(
    user: RequiredUser,
    keyword: str = Query(min_length=1, max_length=32, description="昵称前缀"),
    limit: int = Query(default=10, ge=1, le=20, description="最多返回条数"),
) -> StandardResponse[list[UserBriefOut]]:
    """@ 面板昵称搜索：直连 pptr Postgres 按昵称 / 注册名前缀匹配（Phase 3.1）。

    走前缀匹配 `keyword%`，对索引友好，不会退化成 `%keyword%` 全表扫描。

    搜索结果用于 @ 他人，属他人可见场景：返回 :class:`UserBriefOut`，其私域字段
    由序列化期按访问者身份自动剥离。
    """
    items = await CommentAdminUser.search_by_uname(keyword, limit=limit)
    return StandardResponse(data=[b for b in items])


@router.post("/top", response_model=StandardResponse[CommentTopResp], summary="置顶/取消置顶")
async def comment_top(
    session: SessionDep,
    user: RequiredUser,
    req: CommentTopReq,
) -> StandardResponse[CommentTopResp]:
    """置顶 / 取消置顶一条根评论。

    权限：内容作者（评论区 up_mid）或管理员；全区唯一一条置顶，互斥覆盖。
    """
    try:
        oid_int = int(str(req.oid).strip())
        rpid_int = int(str(req.rpid).strip())
        if oid_int <= 0 or rpid_int <= 0:
            raise ValueError("oid / rpid 不合法")
    except (TypeError, ValueError):
        return StandardResponse(code=400, msg="oid / rpid 不合法")

    is_admin = user.role == "root"
    ok = await CommentService.set_top(
        session, user.mid, oid_int, req.type, rpid_int, is_admin=is_admin, top=req.top
    )
    if not ok:
        return StandardResponse(code=403, msg="无权置顶或评论不存在 / 非根评论")
    subject = await CommentService.get_subject(session, oid_int, req.type)
    return StandardResponse(
        data=CommentTopResp(
            top_rpid=str(subject.top_rpid) if subject and subject.top_rpid else None,
            success=True,
        )
    )


__all__ = ["router"]
