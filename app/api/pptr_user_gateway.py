"""pptr 用户网关接口（`/api/v1/user`）。

**由来**：所有用户接口已整体迁移到 be-message，pptr（Express）退化为纯反向代理，
不再保留任何业务逻辑。

**鉴权**：与本服务其余接口一致，完全信任 pptr 网关注入的 `x-bili-*` 头
（网关已清除客户端伪造值并按 JWT 重写为可信登录态），本服务不做 JWT 校验。
`CurrentUser` 依赖在缺 `x-bili-mid` 时直接抛 401。

**JWT 续期**：pptr 网关通过 `x-bili-jwt` 请求头将原始 JWT 透传给本服务。
`/nav` 和 `/refresh_token` 端点在返回数据的同时，会检查 JWT 是否需要续期
（非当天签发的 token 需要刷新），若需要则签发新 token 并同步刷新 Casdoor token，
通过 **HttpOnly + Secure Cookie（`bili_jwt`）** 下发（不再写入响应体，避免 XSS 读取）。
浏览器直连服务端（be-message 经网关注理）落盘该 Cookie，前端不再使用 localStorage。

**响应**：统一 `StandardResponse`（`code=0` 成功），不再沿用 pptr 旧的
`{code, data, msg, ttl}` 格式。
"""

import os

from typing import Annotated

from bili_common.models import (
    VALID_ROLES,
    VALID_SEX_VALUES,
    PptrUserInfoUpdateParams,
    PptrUserInfoUpdateResult,
    PptrUserNavData,
    PptrUserRoleSetParams,
    PptrUserRoleSetResult,
    PptrUserSearchResult,
    ResponseCode,
    StandardResponse,
    UserSearchParams,
)
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response
from loguru import logger

from app.core.config import settings
from app.core.database import SessionDep, new_pptr_session
from app.dependencies import AdminUser, CurrentUser
from app.models.str_int import StrInt
from app.models.schemas import (
    SpaceInfoResp,
    UserActLogListResp,
    UserExpRecordListResp,
)
from app.services.user import casdoor_service
from app.services.infrastructure import jwt_service
from app.services.message import publisher
from app.services.user.avatar_audit import AvatarAuditService
from app.services.user.avatar_check import verify_avatar_url
from app.services.user.casdoor_service import CasdoorError
from app.services.user.follow import FollowService
from app.services.user.pptr_user import PptrUserService
from app.models.schemas.follow import (
    BlockReq,
    FollowListResp,
    FollowOpResp,
    FollowRelationResp,
)
from pydantic import BaseModel
from app.utils.ip_mask import extract_client_ip

router = APIRouter(prefix="/api/v1/user", tags=["pptr-user-gateway"])


# --- JWT 存储（HttpOnly + Secure Cookie，替代前端 localStorage） ---
# 浏览器直连服务端（be-message 作为 token 签发方，经网关注理时由代理透传 Set-Cookie，
# 浏览器按网关域名落盘）写入 HttpOnly Cookie，前端 JS 无法读取，防 XSS 窃取。
JWT_COOKIE_NAME = "bili_jwt"


def _jwt_cookie_secure() -> bool:
    env = os.getenv("JWT_COOKIE_SECURE")
    if env is not None:
        return env.lower() == "true"
    return settings.app_env == "production"


def set_jwt_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        key=JWT_COOKIE_NAME,
        value=token,
        httponly=True,
        secure=_jwt_cookie_secure(),
        samesite="lax",
        path="/",
        max_age=settings.jwt_expires_seconds,
    )


def clear_jwt_cookie(response: Response) -> None:
    response.set_cookie(
        key=JWT_COOKIE_NAME,
        value="",
        httponly=True,
        secure=_jwt_cookie_secure(),
        samesite="lax",
        path="/",
        max_age=0,
    )


@router.get(
    "/identify",
    response_model=StandardResponse[dict],
    summary="从 JWT 解析用户身份（供网关中间件调用，不依赖 x-bili-mid）",
)
async def identify_user(
    authorization: Annotated[str | None, Header()] = None,
    x_bili_jwt: Annotated[str | None, Header()] = None,
) -> StandardResponse[dict]:
    """从 Authorization Bearer JWT 或 x-bili-jwt 头解析用户身份并查库返回完整身份。

    供 be-gateway 的 userInfoPreFetchMiddleware 调用：
    be-gateway 不再自行解析 JWT，改为调用本端点获取用户身份后注入 x-bili-* 头。

    本端点不依赖 x-bili-mid（避免循环依赖），仅从 JWT 载荷提取 uid，
    随后查 pptr Postgres 取回最新身份信息。

    **为何查库而非直接用 JWT 载荷**：JWT 在签发时快照了 user_name / level / role，
    但这些字段会随后续操作变化（如 Casdoor 登录后用户名冲突被改写为 bili_xxx、
    管理员调整角色、每日登录升级 level）。直接返回 JWT 字段会导致网关注入的
    x-bili-* 头与数据库现状不一致。此外昵称(uname)/签名/性别/邮箱/会员信息
    并不存在于 JWT 中，必须查库才能返回。

    **user_name 与 uname 的区分**（严格对齐数据库语义）：
    - user_name：用户名，存于 TUserInfo.user_name，注册后不可变；
    - uname：昵称，存于 TUserDetail.uname，用户可自行修改。

    返回字段与 PrefetchUserInfo.js 所需的 x-bili-* 头一一对应：
    mid / user_name / level / role / uname / sign / sex / email / vip_status / vip_type。
    """
    token = None
    if authorization and authorization.startswith("Bearer "):
        token = authorization[7:]
    elif x_bili_jwt:
        token = x_bili_jwt

    if not token:
        raise HTTPException(status_code=401, detail="未提供 JWT 令牌")

    payload = jwt_service.decode_token(token)
    if not payload:
        raise HTTPException(status_code=401, detail="JWT 令牌无效或已过期")

    uid = payload.get("uid")
    if uid is None:
        raise HTTPException(status_code=401, detail="JWT 载荷缺少 uid")

    # 查库取最新身份（JWT 里的 user_name/level/role 可能已过时）
    profile = await PptrUserService.get_user_profile(uid=int(uid))
    if profile is None:
        raise HTTPException(status_code=401, detail="用户不存在")
    info, detail, vip, level = profile

    return StandardResponse(data={
        "mid": str(info.uid),
        # 用户名（不可变）← TUserInfo.user_name
        "user_name": info.user_name or "",
        # 等级 ← TUserLevel.current_level
        "level": str(level.current_level) if level and level.current_level is not None else "0",
        # 角色 ← TUserInfo.role
        "role": info.role or "",
        # 以下均来自 TUserDetail（昵称 uname 可变，其余为个人资料）
        "uname": (detail.uname if detail else None) or "",
        "sign": (detail.sign if detail else None) or "",
        "sex": (detail.sex if detail else None) or "",
        "email": (detail.email if detail else None) or "",
        # 会员信息 ← TUserVip
        "vip_status": str(vip.vip_status) if vip and vip.vip_status is not None else "",
        "vip_type": str(vip.vip_type) if vip and vip.vip_type is not None else "",
    })


def parse_user_search_params(
    keyword: str = Query("", description="搜索关键字：昵称 / 注册名 / mid / 邮箱"),
    offset: int = Query(0, ge=0, description="分页偏移量（游标），从 0 开始"),
    limit: int = Query(10, ge=1, le=100, description="单页条数，默认 10，最大 100"),
) -> UserSearchParams:
    """把 query 参数解析为 `UserSearchParams`。

    不直接用 `Depends(UserSearchParams)`：SQLModel 模型作为 FastAPI query 依赖
    会触发 OpenAPI schema 生成 bug（详见 bili_common.models.user_search 文档）。
    """
    return UserSearchParams(keyword=keyword, offset=offset, limit=limit)


@router.get(
    "/nav",
    response_model=StandardResponse[PptrUserNavData],
    summary="获取当前登录用户的导航信息（含等级 / 角色 / 头像）",
)
async def get_user_nav(
    user: CurrentUser,
    request: Request,
    x_bili_jwt: Annotated[str | None, Header()] = None,
    response: Response = None,
) -> StandardResponse[PptrUserNavData]:
    """返回当前登录用户的导航栏展示信息。

    含每日首次登录加经验（幂等）、等级计算、邮件脱敏；
    与 pptr 旧 `get_user_nav_with_level` 行为一致；加经验失败不影响导航返回。

    同时检查 `x-bili-jwt` 头中的 JWT 是否需要续期（非当天签发的 token 需要刷新），
    若需要则签发新 token 并刷新 Casdoor token，通过 **HttpOnly Cookie** 下发（不再写入响应体）。
    """
    uid = int(user.mid)

    # 提取客户端 IP / UA：供「每日首次访问」记录登录行为时落库
    client_ip_v4, client_ip_v6 = extract_client_ip(
        request.headers, peer=request.client.host if request.client else None
    )
    client_ip = client_ip_v4 or client_ip_v6 or ""
    client_ua = request.headers.get("user-agent") or ""

    data = await PptrUserService.get_user_nav_data(
        uid=uid, ip=client_ip, ua=client_ua
    )
    if data is None:
        raise HTTPException(status_code=404, detail="用户不存在")

    # JWT 续期检查
    new_jwt = await _maybe_refresh_jwt(x_bili_jwt, uid)
    if new_jwt:
        # 续期 token 改由 HttpOnly Cookie 下发，不再写入响应体（防 XSS 读取）
        set_jwt_cookie(response, new_jwt)

    return StandardResponse(data=data)


@router.get(
    "/user_info",
    response_model=StandardResponse[dict],
    summary="获取当前登录用户的个人资料（昵称 / 注册名 / 签名 / 性别 / 生日）",
)
async def get_user_info(user: CurrentUser) -> StandardResponse[dict]:
    """返回当前登录用户的个人资料，供「用户基本信息设置」页面回填表单。

    数据来自 pptr Postgres 的 TUserInfo / TUserDetail，与前端
    `User_base_info_config_form` 字段对齐：
    - uname     <- TUserDetail.uname（可改昵称）
    - userid    <- TUserInfo.user_name（注册名，前端展示为「用户名」）
    - usersign  <- TUserDetail.sign（个性签名）
    - sex       <- TUserDetail.sex
    - birthday  <- TUserDetail.birthday（ISO 字符串）
    - mid       <- TUserInfo.uid
    - email     <- TUserDetail.email
    - avatar    <- TUserDetail.avatar
    """
    uid = int(user.mid)
    profile = await PptrUserService.get_user_profile(uid=uid)
    if profile is None:
        raise HTTPException(status_code=404, detail="用户不存在")
    info, detail, _vip, _level = profile

    birthday = ""
    if detail and detail.birthday:
        birthday = detail.birthday.isoformat()

    return StandardResponse(
        data={
            "uname": (detail.uname if detail else "") or "",
            "userid": info.user_name or "",
            "usersign": (detail.sign if detail else "") or "",
            "sex": (detail.sex if detail else "") or "保密",
            "birthday": birthday,
            "mid": str(info.uid),
            "email": (detail.email if detail else "") or "",
            "avatar": (detail.avatar if detail else "") or "",
        }
    )


@router.post(
    "/user_info/update",
    response_model=StandardResponse[PptrUserInfoUpdateResult],
    summary="更新当前登录用户的个人资料",
)
async def update_user_info(
    session: SessionDep,
    user: CurrentUser,
    params: PptrUserInfoUpdateParams,
) -> StandardResponse[PptrUserInfoUpdateResult]:
    """更新昵称 / 签名 / 性别 / 生日 / 头像。

    只能修改本人资料：`uid` 固定取自鉴权身份（`x-bili-mid`），不接受入参覆盖。
    TUserNameRecord 表已移除，不再记录昵称历史。

    头像（`avatar`）为图片 URL（http/https），后端下载校验：1s 内下载完成且 ≤1MB；
    校验通过后**不即时生效**，而是提交头像更换审核（`TUserAvatarAudit` 待审核），
    `avatar_status=pending`，公开头像保持不变，审核通过后才对外展示。
    空串表示不修改头像。
    """
    uid = int(user.mid)

    if params.uname and not (2 <= len(params.uname) <= 24):
        raise HTTPException(status_code=422, detail="昵称需为 2-24 个字！")

    if params.sex and params.sex not in VALID_SEX_VALUES:
        raise HTTPException(status_code=422, detail="性别不正确！")

    avatar_status: str | None = "none"
    if params.avatar:
        ok, reason = await verify_avatar_url(params.avatar)
        if not ok:
            raise HTTPException(status_code=422, detail=reason)

    # 非头像字段即时更新
    updated = await PptrUserService.set_user_detail(
        uid=uid,
        uname=params.uname or "",
        sign=params.usersign or "",
        sex=params.sex or "保密",
        birthday=params.birthday or "",
    )
    if not updated:
        raise HTTPException(status_code=500, detail="更新用户信息失败")

    # 头像：提交更换审核（不写公开头像）
    if params.avatar:
        old_avatar = await AvatarAuditService._current_avatar(uid)
        await AvatarAuditService.submit(
            session,
            uid=uid,
            new_avatar=params.avatar,
            old_avatar=old_avatar,
        )
        avatar_status = "pending"

    return StandardResponse(
        data=PptrUserInfoUpdateResult(
            uid=str(uid),
            updated=True,
            uname_recorded=False,
            avatar_status=avatar_status,
        ),
        msg="更新成功",
    )


# ==================== 黑名单管理 ====================


class UserDeactivateReq(BaseModel):
    """账号注销请求：需二次确认。"""

    confirm: bool = False


@router.get(
    "/blocklist",
    response_model=StandardResponse[FollowListResp],
    summary="我的黑名单列表",
)
async def list_blocklist(
    session: SessionDep,
    user: CurrentUser,
    page_num: int = Query(1, ge=1, description="页码"),
    page_size: int = Query(20, ge=1, le=100, description="每页条数"),
) -> StandardResponse[FollowListResp]:
    """分页返回当前用户拉黑的用户（仅含 mid 与拉黑时间）。"""
    uid = int(user.mid)
    data = await FollowService.list_blocked(
        session, mid=uid, page_num=page_num, page_size=page_size
    )
    return StandardResponse(data=data, msg="ok")


@router.post(
    "/blocklist",
    response_model=StandardResponse[FollowOpResp],
    summary="拉黑用户",
)
async def add_blocklist(
    session: SessionDep,
    user: CurrentUser,
    req: BlockReq,
) -> StandardResponse[FollowOpResp]:
    """将指定 mid 加入黑名单；与关注关系互斥（会解除对方对自己的关注）。"""
    uid = int(user.mid)
    try:
        data = await FollowService.block(session, mid=uid, target_mid=int(req.target_mid))
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    return StandardResponse(data=data, msg="已拉黑")


@router.delete(
    "/blocklist",
    response_model=StandardResponse[FollowOpResp],
    summary="解除拉黑",
)
async def remove_blocklist(
    session: SessionDep,
    user: CurrentUser,
    target_mid: int = Query(..., description="被解除拉黑的用户 mid"),
) -> StandardResponse[FollowOpResp]:
    """将指定 mid 移出黑名单。"""
    uid = int(user.mid)
    try:
        data = await FollowService.unblock(session, mid=uid, target_mid=target_mid)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    return StandardResponse(data=data, msg="已解除拉黑")


@router.get(
    "/blocklist/check",
    response_model=StandardResponse[FollowRelationResp],
    summary="查询与某用户的关系",
)
async def check_blocklist(
    session: SessionDep,
    user: CurrentUser,
    target_mid: int = Query(..., description="目标用户 mid"),
) -> StandardResponse[FollowRelationResp]:
    """查询当前用户与目标用户的双向关系（含是否拉黑 / 被拉黑）。"""
    uid = int(user.mid)
    data = await FollowService.get_relation(session, mid=uid, target_mid=target_mid)
    return StandardResponse(data=data, msg="ok")


# ==================== 账号注销 ====================


@router.post(
    "/deactivate",
    response_model=StandardResponse[str],
    summary="注销当前账户",
)
async def deactivate_account(
    user: CurrentUser,
    req: UserDeactivateReq,
) -> StandardResponse[str]:
    """注销当前登录账户（二次确认后入队异步清理）。

    仅接受 `confirm=true` 的主动注销请求；实际账户与数据清理由 consumer 异步执行。
    """
    if not req.confirm:
        raise HTTPException(status_code=400, detail="请确认注销操作")
    uid = int(user.mid)
    ok = await publisher.publish_user_deactivate(uid)
    if not ok:
        raise HTTPException(status_code=500, detail="注销请求提交失败，请稍后重试")
    return StandardResponse(data=str(uid), msg="注销申请已提交，账户将在后台异步清理")


@router.post(
    "/role/set",
    response_model=StandardResponse[PptrUserRoleSetResult],
    summary="设置用户角色（仅系统管理员 root 可操作）",
)
async def set_user_role(
    user: CurrentUser,
    params: PptrUserRoleSetParams,
) -> StandardResponse[PptrUserRoleSetResult]:
    """把目标用户的角色调整为指定值。

    约束（与 pptr 旧实现一致）：
    - 仅 root 可调用；
    - 不允许修改自己的角色（防止管理员误把自己降级导致系统无管理员）；
    - be-message 服务层额外保护：root 角色不会被降级覆盖。
    """
    if params.role not in VALID_ROLES:
        raise HTTPException(status_code=422, detail=f"非法的角色值：{params.role}")

    if not user.is_root:
        raise HTTPException(
            status_code=403, detail="权限不足：只有系统管理员（root）才能设置用户角色"
        )

    operator_uid = int(user.mid)
    try:
        target_uid = int(params.target_uid)
    except (TypeError, ValueError) as e:
        raise HTTPException(status_code=422, detail="目标用户UID非法") from e

    if operator_uid == target_uid:
        raise HTTPException(status_code=400, detail="不能修改自己的角色")

    target = await PptrUserService.get_user_profile(uid=target_uid)
    if target is None:
        raise HTTPException(status_code=404, detail="目标用户不存在")
    target_info = target[0]

    ok = await PptrUserService.set_user_role(uid=target_uid, role=params.role)
    if not ok:
        raise HTTPException(
            status_code=400, detail="目标用户角色更新失败（可能受 root 保护）"
        )

    role_description = settings.level_role_description.get(params.role, "普通用户 (Lv0)")
    return StandardResponse(
        data=PptrUserRoleSetResult(
            target_uid=str(target_uid),
            target_user_name=target_info.user_name or "",
            role=params.role,
            role_name=params.role,
            role_description=role_description,
        ),
        msg=f"已将用户【{target_info.user_name}】的角色设置为【{role_description}】",
    )


@router.get(
    "/search",
    response_model=StandardResponse[PptrUserSearchResult],
    summary="按昵称 / 注册名 / mid / 邮箱搜索用户（仅 root）",
)
async def search_users(
    user: CurrentUser,
    params: Annotated[UserSearchParams, Depends(parse_user_search_params)],
) -> StandardResponse[PptrUserSearchResult]:
    """搜索用户，供「授予管理端权限」等管理场景先行查找目标用户。

    仅 root 可调用；评论区 @ 提及请使用 `/api/v1/comment/at/search`
    （按昵称搜索，登录即可）。分页采用 `offset + limit`，`has_more` 指示是否还有下一页。
    """
    if not user.is_root:
        raise HTTPException(status_code=403, detail="只有 root 用户才能进行用户查找")

    if not params.keyword:
        raise HTTPException(status_code=422, detail="keyword 不能为空")

    items, has_more = await PptrUserService.search_users(
        params.keyword, offset=params.offset, limit=params.limit
    )
    return StandardResponse(data=PptrUserSearchResult(items=items, has_more=has_more))


def parse_record_query_params(
    offset: int = Query(0, ge=0, description="分页偏移量（游标），从 0 开始"),
    limit: int = Query(10, ge=1, le=100, description="单页条数，默认 10，最大 100"),
    days: int = Query(7, ge=1, le=7, description="时间窗口天数，最多 7（仅展示最近一周）"),
) -> dict:
    """把「我的记录」两个接口共用的 query 参数解析为 dict。"""
    return {"offset": offset, "limit": limit, "days": days}


@router.get(
    "/act-log",
    response_model=StandardResponse[UserActLogListResp],
    summary="获取当前登录用户最近 7 天的登录 / 行为记录",
)
async def get_user_act_log(
    user: CurrentUser,
    query: Annotated[dict, Depends(parse_record_query_params)],
) -> StandardResponse[UserActLogListResp]:
    """返回当前登录用户最近一周的登录记录（TUserActInfoLog，仅本人）。

    按 `createdAt` 倒序，`offset + limit` 分页，`has_more` 指示是否还有下一页。
    仅返回时间 / IP / UA / 行为类型，不透出 headers 全量 JSON。
    """
    uid = int(user.mid)
    data = await PptrUserService.list_act_log(
        uid=uid,
        offset=query["offset"],
        limit=query["limit"],
        days=query["days"],
    )
    return StandardResponse(data=data)


@router.get(
    "/exp-record",
    response_model=StandardResponse[UserExpRecordListResp],
    summary="获取当前登录用户最近 7 天的经验变动记录",
)
async def get_user_exp_record(
    user: CurrentUser,
    query: Annotated[dict, Depends(parse_record_query_params)],
) -> StandardResponse[UserExpRecordListResp]:
    """返回当前登录用户最近一周的经验记录（TUserExpRecord，仅本人）。

    按 `createdAt` 倒序，`offset + limit` 分页，`has_more` 指示是否还有下一页。
    `action_type` 为 int，同时返回其可读名称（对齐 ExpActionType，如 daily_login）。
    """
    uid = int(user.mid)
    data = await PptrUserService.list_exp_record(
        uid=uid,
        offset=query["offset"],
        limit=query["limit"],
        days=query["days"],
    )
    return StandardResponse(data=data)


# ==================== 用户空间信息（对标 B 站 acc/info）====================


def _resolve_space_viewer(x_bili_mid: str | None) -> int | None:
    """解析可选登录态 viewer（未登录返回 None，空间信息公开可读）。"""
    if not x_bili_mid:
        return None
    try:
        mid = int(x_bili_mid)
    except (TypeError, ValueError):
        return None
    return mid if mid > 0 else None


@router.get(
    "/space/info",
    response_model=StandardResponse[SpaceInfoResp],
    response_model_exclude_none=True,
    summary="用户空间完整资料（对标 B 站 /x/space/wbi/acc/info）",
)
async def get_space_info(
    session: SessionDep,
    mid: StrInt = Query(..., description="目标用户 mid（对标 B 站 acc/info 的 mid 参数，StrInt 兼容前端 str 传参）"),
    x_bili_mid: str | None = Header(default=None),
) -> StandardResponse[SpaceInfoResp]:
    """返回单个用户的完整空间资料（对标 B 站 `/x/space/wbi/acc/info?mid=`）。

    - 公开可读（未登录也可访问）；登录时附带 `is_followed` 关注态与黑名单判断；
    - **用户不存在**返回专用错误码 `USER_NOT_FOUND`（而非空数据兜底）；
    - **黑名单互访拒绝**：当前登录用户与目标存在任一向黑名单关系（已拉黑 / 被拉黑）
      时返回 `403`，拒绝返回空间数据（本人访问自己空间除外）。
    """
    if mid <= 0:
        return StandardResponse(code=400, msg="mid 不合法")
    viewer = _resolve_space_viewer(x_bili_mid)

    # 黑名单互访拒绝（本人除外）：已拉黑目标或被目标拉黑均不可访问其空间
    if viewer is not None and viewer != mid:
        blocked = await FollowService.is_blocked_relation(session, viewer, mid)
        if blocked:
            return StandardResponse(code=403, msg="对方已将你加入黑名单，无法访问其空间")

    data = await PptrUserService.get_space_info(uid=int(mid))
    if data is None:
        return StandardResponse(
            code=int(ResponseCode.USER_NOT_FOUND), msg="用户不存在", data=None
        )

    # 补充关注态与本人标记（依赖 MySQL 关注关系，路由层补上）
    data.is_followed = viewer is not None and await FollowService.is_following(
        session, viewer, mid
    )
    data.is_self = viewer == mid
    return StandardResponse(data=data)


# ==================== 注销账号（P12，2.15.0 / 2.15.1）====================


async def _submit_deactivate(uid: int) -> bool:
    """校验 uid 合法后投递注销 MQ（异步执行删除）。

    Returns:
        bool: 投递是否成功。
    """
    if not uid or uid <= 0:
        raise ValueError("mid 不合法")
    return await publisher.publish_user_deactivate(uid)


@router.post(
    "/deactivate",
    response_model=StandardResponse,
    summary="注销当前账号（投递注销，异步删除账号及业务数据）",
)
async def deactivate_self(user: CurrentUser) -> StandardResponse:
    """注销当前登录账号：投递注销消息，由消费者异步物理删除 pptr 四表 +
    彻底清除 be-message 业务数据（不可恢复）。

    **Casdoor 不管**（不同步禁用）。注销为异步执行，接口提交成功后返回；
    前端应清除本地登录态并跳登录页。
    """
    try:
        ok = await _submit_deactivate(int(user.mid))
    except ValueError:
        return StandardResponse(code=400, msg="mid 不合法")
    if not ok:
        return StandardResponse(code=500, msg="注销提交失败，请稍后重试")
    return StandardResponse(data=None, msg="注销已提交，正在处理")


@router.post(
    "/admin/deactivate",
    response_model=StandardResponse,
    summary="管理端注销指定用户（投递注销）",
)
async def deactivate_user(
    admin: AdminUser,
    target_mid: StrInt = Query(..., description="目标用户 mid（雪花 ID，StrInt 兼容前端 str 传参）"),
) -> StandardResponse:
    """管理端注销指定用户（root / 管理员）：投递注销消息，异步删除其账号及业务数据。"""
    try:
        ok = await _submit_deactivate(target_mid)
    except ValueError:
        return StandardResponse(code=400, msg="mid 不合法")
    if not ok:
        return StandardResponse(code=500, msg="注销提交失败，请稍后重试")
    return StandardResponse(data=None, msg="注销已提交，正在处理")


# ==================== JWT 续期 & Casdoor 等新增端点 ====================


async def _maybe_refresh_jwt(x_bili_jwt: str | None, uid: int) -> str | None:
    """检查 JWT 是否需要续期，若需要则签发新 token 并刷新 Casdoor token。

    Args:
        x_bili_jwt: 网关传来的原始 JWT
        uid: 用户 ID

    Returns:
        新签发的 JWT，无需续期返回 None
    """
    if not x_bili_jwt:
        return None

    payload = jwt_service.decode_token(x_bili_jwt)
    if not jwt_service.is_jwt_expired_today(payload):
        return None

    # 需要续期：签发新 JWT
    user_name = payload.get("user_name", "")
    level = payload.get("level", "0")
    role = payload.get("role", "0")
    new_token = jwt_service.create_token(
        user_name=user_name,
        uid=uid,
        level=level,
        role=role,
    )

    # 同步刷新 Casdoor OAuth token
    try:
        await casdoor_service.refresh_casdoor_token(uid=uid)
    except Exception as e:
        logger.warning(f"[JWT] 用户 {uid} 的 Casdoor token 刷新失败（不影响 JWT 续期）: {e}")

    return new_token


@router.post(
    "/refresh_token",
    response_model=StandardResponse[dict],
    summary="刷新当前登录用户的 JWT 令牌",
)
async def refresh_token(
    user: CurrentUser,
    request: Request,
    x_bili_jwt: Annotated[str | None, Header()] = None,
    response: Response = None,
) -> StandardResponse[dict]:
    """刷新当前登录用户的 JWT 令牌，同时同步刷新 Casdoor OAuth token。

    与 pptr 旧 `refresh_token` 行为一致：从用户数据库读取最新信息
    （含 level / role），签发新 token 并刷新 Casdoor token。
    """
    uid = int(user.mid)

    # 获取用户最新信息（含 level / role）
    profile = await PptrUserService.get_user_profile(uid=uid)
    if profile is None:
        raise HTTPException(status_code=404, detail="用户不存在")
    info, _detail, _vip, _level = profile

    # 签发新 JWT
    new_token = jwt_service.create_token(
        user_name=info.user_name or "",
        uid=uid,
        level=str(_level.current_level) if _level and _level.current_level else "0",
        role=info.role or "0",
    )

    # 同步刷新 Casdoor OAuth token（best-effort）
    try:
        await casdoor_service.refresh_casdoor_token(uid=uid)
    except Exception as e:
        logger.warning(f"[refresh_token] 用户 {uid} 的 Casdoor token 刷新失败: {e}")

    # 新 token 通过 HttpOnly Cookie 下发（替代响应体，避免 XSS 读取）
    set_jwt_cookie(response, new_token)
    return StandardResponse(
        data={
            "uid": str(uid),
            "user_name": info.user_name or "",
        },
        msg="刷新成功！",
    )


@router.get(
    "/casdoor/info",
    response_model=StandardResponse,
    summary="获取当前登录用户在 Casdoor 的完整信息（如积分 score、余额等）",
)
async def get_casdoor_user_info(
    user: CurrentUser,
) -> StandardResponse:
    """获取当前登录用户在 Casdoor 侧的完整信息。

    仅允许查询本人。优先使用 pptr Postgres TUserInfo.pwd 存的 Casdoor access_token
    （用户模式 Bearer 调用，能取到 score / balance 等私有字段）；
    token 缺失或调用失败时回退 service 模式（clientId/clientSecret）。
    """
    uid = int(user.mid)

    casdoor_user = await casdoor_service.get_casdoor_user_as_user(
        uid=uid, user_name=user.user_name
    )
    if casdoor_user is None:
        raise HTTPException(status_code=404, detail="未找到 Casdoor 用户信息")

    return StandardResponse(data=casdoor_user, msg="获取成功")


@router.post(
    "/logout",
    response_model=StandardResponse,
    summary="用户退出登录",
)
async def logout(
    user: CurrentUser,
    x_bili_jwt: Annotated[str | None, Header()] = None,
) -> StandardResponse:
    """用户退出登录。

    注意：本服务不维护 JWT 黑名单（签发的 token 有效期内仍可被使用）。
    前端应在收到成功响应后删除本地存储的 JWT token。
    如果需更强的安全性，后续可引入 DB 黑名单或 Redis 机制。
    """
    return StandardResponse(msg="退出登录成功")


# ==================== Casdoor 登录回调（由 pptr 代理）====================

from fastapi.responses import JSONResponse, RedirectResponse


@router.get(
    "/casdoor/callback",
    summary="Casdoor OAuth2 登录回调",
    description=(
        "Casdoor 登录成功后重定向到本端点，携带授权码 code。"
        "本端点处理：exchange code → 获取/创建用户 → 签发 JWT → 重定向到前端。"
        "前端回调地址：FRONTEND_URL/app/casdoor-callback?token=xxx&uid=xxx&user_name=xxx"
    ),
    include_in_schema=True,
)
async def casdoor_callback(
    code: str | None = Query(None, description="Casdoor 授权码"),
    state: str | None = Query(None, description="OAuth state（本服务不校验，仅透传）"),
    request: Request = None,
):
    """Casdoor OAuth2 登录回调端点。

    流程（与 pptr CasdoorService.handleCasdoorCallback 对齐）：
    1. 用授权码换取 Casdoor OAuth token（access_token + refresh_token）
    2. 从 access_token JWT 中解析用户信息
    3. 检查本地用户是否存在，不存在则创建
    4. 将 access_token 写入 TUserInfo.pwd（便于后续直接调用 Casdoor API）
    5. 签发本地 JWT
    6. 记录登录活动
    7. 重定向到前端，携带 token / uid / user_name
    """
    if not code:
        return JSONResponse(
            status_code=400,
            content={"code": -1, "msg": "缺少授权码 code", "data": None},
        )

    # 3. 获取客户端 IP / UA（从代理头中提取）
    client_ip = request.headers.get("x-forwarded-for", "").split(",")[0].strip() if request else None
    client_ua = request.headers.get("user-agent") if request else None

    try:
        # 1. 获取 OAuth token
        oauth_token = await casdoor_service.get_oauth_token(code)
        if not oauth_token.access_token:
            logger.error("[Casdoor callback] 获取 OAuth token 失败：响应中无 access_token")
            return JSONResponse(
                status_code=400,
                content={
                    "code": ResponseCode.CASDOOR_OAUTH_ERROR,
                    "msg": "获取 Casdoor OAuth token 失败",
                    "data": None,
                },
            )

        # 2. 解析用户信息
        casdoor_user = casdoor_service.get_casdoor_user_info_from_token(oauth_token.access_token)
        if not casdoor_user:
            logger.error("[Casdoor callback] 解析 Casdoor JWT 失败")
            return JSONResponse(
                status_code=400,
                content={
                    "code": ResponseCode.CASDOOR_TOKEN_PARSE_FAILED,
                    "msg": "解析 Casdoor 用户信息失败",
                    "data": None,
                },
            )

        username = casdoor_user.name
        if not username:
            return JSONResponse(
                status_code=400,
                content={
                    "code": ResponseCode.CASDOOR_USER_NOT_FOUND,
                    "msg": "Casdoor 用户信息中缺少 name/email",
                    "data": None,
                },
            )

        # 4. 检查/创建本地用户 + 记录登录活动（共享同一 session，失败则回滚）
        async with new_pptr_session() as session:
            local_user = await casdoor_service.create_local_user_from_casdoor(
                username,
                oauth_token,
                email=casdoor_user.email or "",
                uname=casdoor_user.display_name or "",
                ip=client_ip,
                ua=client_ua,
                session=session,
            )
            if not local_user:
                return JSONResponse(
                    status_code=500,
                    content={
                        "code": ResponseCode.CASDOOR_CREATE_USER_FAILED,
                        "msg": "创建/获取本地用户失败",
                        "data": None,
                    },
                )

            # 5. 记录登录活动（与用户创建共享同一 session）
            await casdoor_service.record_login_activity(
                local_user.uid,
                ip=client_ip,
                ua=client_ua,
                session=session,
            )
    except CasdoorError as e:
        logger.error(f"[Casdoor callback] Casdoor 错误: {e.error} - {e.error_description}")
        return JSONResponse(
            status_code=400,
            content={
                "code": ResponseCode.CASDOOR_OAUTH_ERROR,
                "msg": e.error_description or e.error,
                "data": {"error": e.error, "error_description": e.error_description},
            },
        )
    except Exception as e:
        logger.exception(f"[Casdoor callback] 登录流程异常：{e}")
        return JSONResponse(
            status_code=500,
            content={
                "code": ResponseCode.INTERNAL_ERROR,
                "msg": f"登录流程异常，请重试\n{e}",
                "data": None,
            },
        )

    # 6. 签发本地 JWT
    jwt_token = jwt_service.create_token(
        user_name=username,
        uid=local_user.uid,
        level=local_user.level,
        role=local_user.role,
    )

    # 7. 重定向到前端
    # token 不再出现在 URL（避免日志 / Referer 泄露），改为 HttpOnly Cookie 下发
    frontend_url = settings.frontend_url or ""
    redirect_target = (
        f"{frontend_url}/app/casdoor-callback"
        f"?uid={local_user.uid}"
        f"&user_name={local_user.user_name}"
    )
    resp = RedirectResponse(url=redirect_target, status_code=302)
    set_jwt_cookie(resp, jwt_token)
    return resp

