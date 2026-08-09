"""评论系统 HTTP 接口（/api/v1/comment）。

对应 B 站评论系统架构中的 `reply-interface` 对外 REST 层，覆盖 Phase 1 的基础读写：

- `POST /add`    发表评论（一级 / 楼中楼），登录态；同事务落索引 + 正文 + 计数。
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
from app.models.enums import BanServiceEnum, CommentSortEnum, CommentTypeEnum
from app.models.schemas import (
    CommentActionReq,
    CommentActionResp,
    CommentAddReq,
    CommentAddResp,
    CommentCountResp,
    CommentDelReq,
    CommentItem,
    CommentListResp,
    CommentOperationResp,
    CommentSubListResp,
    CommentTopReq,
    CommentTopResp,
    CommentUserBrief,
)
from app.services.comment import CommentService
from app.services.ban_service import BanService
from app.services.comment_action import CommentActionService
from app.services.comment_read import CommentReadService
from app.services.pptr_user import PptrUserService
from app.utils.ip_mask import extract_client_ip

router = APIRouter(prefix="/api/v1/comment", tags=["comment"])


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
    """
    # 封禁校验：被封禁「评论」服务的用户禁止发表评论
    if await BanService.is_banned(session, user.mid, BanServiceEnum.COMMENT.value):
        return StandardResponse(code=403, msg="该账号已被封禁评论功能，无法发表评论")

    ip_v4, ip_v6 = extract_client_ip(
        dict(request.headers),
        request.client.host if request.client else None,
    )
    try:
        data = await CommentService.add(
            session,
            user.mid,
            req,
            uname=user.uname,
            ip_v4=ip_v4,
            ip_v6=ip_v6,
            user_agent=user_agent,
        )
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
    type: CommentTypeEnum = Query(description="业务实体类型"),
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

    data = await CommentReadService.list_main(
        session,
        oid_int,
        type,
        sort=sort,
        page_num=page_num,
        page_size=page_size,
        viewer_mid=_viewer,
        focus_rpid=focus_int,
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
    type: CommentTypeEnum = Query(description="业务实体类型"),
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


# ==================== 楼中楼 / 互动 / @ / 置顶 ====================


@router.get("/reply", response_model=StandardResponse[CommentSubListResp], summary="楼中楼展开")
async def reply_list(
    session: SessionDep,
    _viewer: int | None = Depends(resolve_optional_viewer),
    root: str = Query(description="根评论rpid（字符串）"),
    oid: str = Query(description="业务实体id（字符串）"),
    type: CommentTypeEnum = Query(description="业务实体类型"),
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


@router.get("/at/search", response_model=StandardResponse[list[CommentUserBrief]], summary="@用户搜索")
async def at_search(
    user: RequiredUser,
    keyword: str = Query(min_length=1, max_length=32, description="昵称前缀"),
    limit: int = Query(default=10, ge=1, le=20, description="最多返回条数"),
) -> StandardResponse[list[CommentUserBrief]]:
    """@ 面板昵称搜索：直连 pptr Postgres 按昵称 / 注册名前缀匹配（Phase 3.1）。

    走前缀匹配 `keyword%`，对索引友好，不会退化成 `%keyword%` 全表扫描。
    """
    items = await PptrUserService.search_by_uname(keyword, limit=limit)
    return StandardResponse(data=items)


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
