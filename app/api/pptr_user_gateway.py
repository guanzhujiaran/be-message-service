"""pptr 用户网关接口（`/api/v1/user`）。

**由来**：所有用户接口已整体迁移到 be-message，pptr（Express）退化为纯反向代理，
不再保留任何业务逻辑。

**鉴权**：与本服务其余接口一致，完全信任 pptr 网关注入的 `x-bili-*` 头
（网关已清除客户端伪造值并按 JWT 重写为可信登录态），本服务不做 JWT 校验。
`CurrentUser` 依赖在缺 `x-bili-mid` 时直接抛 401。

**JWT 续期**：pptr 网关通过 `x-bili-jwt` 请求头将原始 JWT 透传给本服务。
`/nav` 和 `/refresh_token` 端点在返回数据的同时，会检查 JWT 是否需要续期
（非当天签发的 token 需要刷新），若需要则签发新 token 并同步刷新 Casdoor token，
将新 token 注入响应体的 `jwt_token` 字段。

**响应**：统一 `StandardResponse`（`code=0` 成功），不再沿用 pptr 旧的
`{code, data, msg, ttl}` 格式。
"""

from fastapi import APIRouter, Depends, HTTPException, Query, Header, Request
from typing import Annotated
from loguru import logger

from app.dependencies import CurrentUser
from app.core.config import settings
from app.core.database import new_pptr_session
from app.services.casdoor_service import CasdoorError
from app.services.pptr_user import PptrUserService
from app.services import jwt_service, casdoor_service
from bili_common.models import (
    PptrUserInfoUpdateParams,
    PptrUserInfoUpdateResult,
    PptrUserNavData,
    PptrUserRoleSetParams,
    PptrUserRoleSetResult,
    PptrUserSearchResult,
    ResponseCode,
    StandardResponse,
    UserSearchParams,
    VALID_ROLES,
    VALID_SEX_VALUES,
)

router = APIRouter(prefix="/api/v1/user", tags=["pptr-user-gateway"])


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
) -> StandardResponse[PptrUserNavData]:
    """返回当前登录用户的导航栏展示信息。

    含每日首次登录加经验（幂等）、等级计算、邮件脱敏；
    与 pptr 旧 `get_user_nav_with_level` 行为一致；加经验失败不影响导航返回。

    同时检查 `x-bili-jwt` 头中的 JWT 是否需要续期（非当天签发的 token 需要刷新），
    若需要则签发新 token 并刷新 Casdoor token，将新 token 注入 `jwt_token` 字段。
    """
    uid = int(user.mid)

    data = await PptrUserService.get_user_nav_data(uid=uid)
    if data is None:
        raise HTTPException(status_code=404, detail="用户不存在")

    # JWT 续期检查
    new_jwt = await _maybe_refresh_jwt(x_bili_jwt, uid)
    if new_jwt:
        data.jwt_token = new_jwt

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
    user: CurrentUser,
    params: PptrUserInfoUpdateParams,
) -> StandardResponse[PptrUserInfoUpdateResult]:
    """更新昵称 / 签名 / 性别 / 生日。

    只能修改本人资料：`uid` 固定取自鉴权身份（`x-bili-mid`），不接受入参覆盖。
    TUserNameRecord 表已移除，不再记录昵称历史。
    """
    uid = int(user.mid)

    if params.sex and params.sex not in VALID_SEX_VALUES:
        raise HTTPException(status_code=422, detail="性别不正确！")

    updated = await PptrUserService.set_user_detail(
        uid=uid,
        uname=params.uname or "",
        sign=params.usersign or "",
        sex=params.sex or "保密",
        birthday=params.birthday or "",
    )
    if not updated:
        raise HTTPException(status_code=500, detail="更新用户信息失败")

    return StandardResponse(
        data=PptrUserInfoUpdateResult(
            uid=str(uid), updated=True, uname_recorded=False
        ),
        msg="更新成功",
    )


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
    """搜索用户，供「授予管理端权限」等场景先行查找目标用户。

    仅 root 可调用。分页采用 `offset + limit`，`has_more` 指示是否还有下一页。
    """
    if not user.is_root:
        raise HTTPException(status_code=403, detail="只有 root 用户才能进行用户查找")

    if not params.keyword:
        raise HTTPException(status_code=422, detail="keyword 不能为空")

    items, has_more = await PptrUserService.search_users(
        params.keyword, offset=params.offset, limit=params.limit
    )
    return StandardResponse(data=PptrUserSearchResult(items=items, has_more=has_more))


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

    return StandardResponse(
        data={
            "uid": str(uid),
            "user_name": info.user_name or "",
            "jwt_token": new_token,
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

    仅允许查询本人。自动从 pptr Postgres TUserInfo.pwd 取回 Casdoor access_token，
    以「用户调用」模式查询；若库中无 token 则回退为「service 调用」模式。
    """
    uid = int(user.mid)

    casdoor_user = await casdoor_service.get_casdoor_user_as_user(uid=uid)
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

from fastapi.responses import RedirectResponse, JSONResponse


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
    frontend_url = settings.frontend_url or ""
    redirect_target = (
        f"{frontend_url}/app/casdoor-callback"
        f"?token={jwt_token}"
        f"&uid={local_user.uid}"
        f"&user_name={local_user.user_name}"
    )
    return RedirectResponse(url=redirect_target, status_code=302)

