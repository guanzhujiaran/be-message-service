"""Casdoor OAuth 服务（用户网关下沉 be-message）。

与 pptr CasdoorService.js 行为对齐，提供：
- Casdoor token 刷新（refresh_token grant）
- Casdoor 登录回调（授权码换 token + 创建本地用户）
- Casdoor 用户信息查询
- token 从 pptr Postgres TUserInfo.pwd 字段的读写

所有 Casdoor API 调用统一通过 AsyncCasdoorSDK 进行，不再手写 HTTP 请求。
所有函数不吞异常，由调用方统一处理；支持传入 session 实现同一事务回滚。
返回值全部使用 SQLModel 类型标注，不再使用裸 dict。

注意：Casdoor 登录回调由 pptr 代理到本服务（/api/v1/casdoor/callback → /api/v1/user/casdoor/callback），
前端 Casdoor SDK 的 redirectPath 保持 /api/v1/casdoor/callback 不变。
"""

import json
import uuid as _uuid

from casdoor import AsyncCasdoorSDK
from loguru import logger
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.core.config import settings
from app.core.database import new_pptr_session
from app.models.casdoor import (
    CasdoorApiUser,
    CasdoorJwtUser,
    CasdoorOAuthToken,
    LocalUserResult,
)
from app.models.pptr_db import PptrUserActInfoLog, PptrUserDetail
from app.models.pptr_user import PptrUserInfo
from app.services.pptr_user import PptrUserService


class CasdoorError(Exception):
    """Casdoor API 返回的错误，携带原始 error / error_description 供前端展示。"""

    def __init__(self, error: str, error_description: str = ""):
        self.error = error
        self.error_description = error_description
        super().__init__(f"{error}: {error_description}" if error_description else error)


def _get_sdk() -> AsyncCasdoorSDK:
    """根据 settings 创建 AsyncCasdoorSDK 实例。

    Returns:
        SDK 实例

    Raises:
        ValueError: casdoor_endpoint 未配置
    """
    if not settings.casdoor_endpoint:
        raise ValueError("casdoor_endpoint 未配置")
    return AsyncCasdoorSDK(
        endpoint=settings.casdoor_endpoint,
        client_id=settings.casdoor_client_id,
        client_secret=settings.casdoor_client_secret,
        certificate=settings.casdoor_certificate,
        org_name=settings.casdoor_organization,
        application_name=settings.casdoor_application,
    )


async def _get_casdoor_token_from_db(
    *,
    uid: int | str | None = None,
    user_name: str | None = None,
    session: AsyncSession | None = None,
) -> CasdoorOAuthToken | None:
    """从 pptr Postgres TUserInfo.pwd 读取 Casdoor OAuth token 对象。

    pwd 字段现仅存储 access_token 字符串；兼容旧版 JSON 格式（含 refresh_token 等）。

    Returns:
        CasdoorOAuthToken 实例，或 None
    """
    if not uid and not user_name:
        return None

    async def _run(s: AsyncSession) -> CasdoorOAuthToken | None:
        stmt = select(PptrUserInfo)
        if uid:
            stmt = stmt.where(PptrUserInfo.uid == int(uid))
        elif user_name:
            stmt = stmt.where(PptrUserInfo.user_name == user_name)
        row = (await s.exec(stmt)).first()
        if not row or not row.pwd:
            return None
        # 尝试解析 JSON 格式
        try:
            parsed = json.loads(row.pwd)
            if isinstance(parsed, dict) and parsed.get("access_token"):
                return CasdoorOAuthToken.model_validate(parsed)
        except (json.JSONDecodeError, TypeError):
            pass
        # 旧格式：纯字符串
        return CasdoorOAuthToken(access_token=row.pwd)

    if session is not None:
        return await _run(session)
    async with new_pptr_session() as s:
        return await _run(s)


async def get_refresh_token_from_db(
    *,
    uid: int | str,
    session: AsyncSession | None = None,
) -> str | None:
    """从 pptr Postgres TUserInfo.pwd 读取 Casdoor refresh_token。

    注意：pwd 现仅存 access_token，refresh_token 不再落库，故新用户始终返回 None。
    仅对历史遗留的 JSON 格式 pwd 仍可读出 refresh_token。

    Args:
        uid: 本地用户 uid
        session: 可选数据库会话

    Returns:
        refresh_token 字符串，未找到返回 None
    """
    token_data = await _get_casdoor_token_from_db(uid=uid, session=session)
    if token_data:
        return token_data.refresh_token or None
    return None


async def get_access_token_from_db(
    *,
    uid: int | str | None = None,
    user_name: str | None = None,
    session: AsyncSession | None = None,
) -> str | None:
    """从 pptr Postgres TUserInfo.pwd 读取 Casdoor access_token。

    pwd 字段现仅存储 access_token 字符串（兼容旧版 JSON 格式，会从中提取 access_token）。
    供后续以用户身份直接调用 Casdoor API 使用。

    Args:
        uid: 本地用户 uid
        user_name: 本地用户登录名（uid 为空时使用）
        session: 可选数据库会话

    Returns:
        access_token 字符串，未找到返回 None
    """
    token_data = await _get_casdoor_token_from_db(
        uid=uid, user_name=user_name, session=session
    )
    if token_data:
        return token_data.access_token or None
    return None


async def refresh_casdoor_token(
    uid: int | str,
    session: AsyncSession | None = None,
) -> str | None:
    """刷新指定用户的 Casdoor OAuth token。

    流程：
    1. 从 DB 读取 refresh_token（仅历史 JSON 格式 pwd 可读出；新 pwd 只存 access_token，无 refresh_token）
    2. 通过 SDK 调用 Casdoor OAuth2 token refresh 端点
    3. 将新 access_token 写回 TUserInfo.pwd

    注意：pwd 现仅存 access_token，新用户无 refresh_token 时本函数会跳过刷新（返回 None）。

    Args:
        uid: 本地用户 uid
        session: 可选数据库会话

    Returns:
        新的 access_token，刷新失败返回 None
    """
    refresh_token = await get_refresh_token_from_db(uid=uid, session=session)
    if not refresh_token:
        logger.info(f"[Casdoor] 用户 {uid} 没有 refresh_token，跳过 Casdoor token 刷新")
        return None

    sdk = _get_sdk()
    new_token = await sdk.refresh_token_request(refresh_token)
    logger.info(f"[Casdoor] SDK refresh_token_request 原始返回: {new_token}")

    if "error" in new_token:
        logger.warning(
            f"[Casdoor] 用户 {uid} 的 Casdoor token 刷新失败: "
            f"{new_token.get('error')} - {new_token.get('error_description', '')}"
        )
        return None

    token_model = CasdoorOAuthToken.model_validate(new_token)

    if not token_model.access_token:
        logger.warning(f"[Casdoor] 用户 {uid} 的 Casdoor token 刷新失败: 响应中无 access_token")
        return None

    # 写回 TUserInfo.pwd（仅存 access_token，便于后续直接调用 Casdoor API）
    async def _update(s: AsyncSession) -> None:
        stmt = select(PptrUserInfo).where(PptrUserInfo.uid == int(uid))
        row = (await s.exec(stmt)).first()
        if row:
            row.pwd = token_model.access_token
            s.add(row)
            await s.commit()

    if session is not None:
        await _update(session)
    else:
        async with new_pptr_session() as s:
            await _update(s)

    logger.info(f"[Casdoor] 用户 {uid} 的 Casdoor token 刷新成功")
    return token_model.access_token


async def get_casdoor_user_by_user_name(user_name: str) -> CasdoorApiUser | None:
    """通过 SDK 调用 Casdoor get-user 接口获取用户信息。

    使用 SDK 内建的 Basic Auth（client_id:client_secret）进行 service 级别调用。

    Args:
        user_name: Casdoor 用户名（即本地 TUserInfo.user_name）

    Returns:
        CasdoorApiUser 实例，未找到返回 None
    """
    sdk = _get_sdk()
    raw = await sdk.get_user(user_name)
    logger.info(f"[Casdoor] SDK get_user 原始返回: {raw}")
    if not raw or "error" in raw:
        return None
    return CasdoorApiUser.model_validate(raw)


async def get_casdoor_user_as_user(
    *,
    uid: int | str,
    user_name: str | None = None,
    session: AsyncSession | None = None,
) -> CasdoorApiUser | None:
    """以用户身份获取 Casdoor 信息。

    流程：
    1. 解析用户登录名（按 uid 查库）
    2. 通过 SDK 的 Basic Auth 调用 get-user

    Args:
        uid: 本地用户 uid
        user_name: 可选的用户登录名，缺省时按 uid 查库
        session: 可选数据库会话

    Returns:
        CasdoorApiUser 实例，未找到返回 None
    """
    # 1. 解析用户登录名
    resolved_name = user_name
    if not resolved_name:
        async def _get_name(s: AsyncSession) -> str | None:
            stmt = select(PptrUserInfo.user_name).where(PptrUserInfo.uid == int(uid))
            return (await s.exec(stmt)).first()
        if session is not None:
            resolved_name = await _get_name(session)
        else:
            async with new_pptr_session() as s:
                resolved_name = await _get_name(s)
    if not resolved_name:
        return None

    # 2. 通过 SDK 获取用户信息
    return await get_casdoor_user_by_user_name(resolved_name)


# ==================== Casdoor 登录回调（由 pptr 代理到本服务）====================


async def get_oauth_token(code: str) -> CasdoorOAuthToken:
    """通过 SDK 以授权码从 Casdoor 获取 OAuth token。

    Args:
        code: OAuth2 授权码（Casdoor 回调时携带）

    Returns:
        CasdoorOAuthToken 实例

    Raises:
        ValueError: casdoor_endpoint 未配置
        Exception: SDK 调用失败
    """
    sdk = _get_sdk()
    raw = await sdk.get_oauth_token(code=code)
    logger.info(f"[Casdoor] SDK get_oauth_token 原始返回: {raw}")

    if "error" in raw:
        raise CasdoorError(raw.get("error", ""), raw.get("error_description", ""))

    return CasdoorOAuthToken.model_validate(raw)


def get_casdoor_user_info_from_token(access_token: str) -> CasdoorJwtUser:
    """通过 SDK 解析 Casdoor access_token（JWT）获取用户信息。

    使用 SDK 内建的 parse_jwt_token 进行证书验签，确保 token 真实有效。

    Args:
        access_token: Casdoor 签发的 JWT access_token

    Returns:
        CasdoorJwtUser 实例

    Raises:
        ValueError: casdoor_endpoint 未配置
        Exception: JWT 解析失败
    """
    sdk = _get_sdk()
    raw = sdk.parse_jwt_token(access_token)
    logger.info(f"[Casdoor] SDK parse_jwt_token 原始返回: {raw}")
    return CasdoorJwtUser.model_validate(raw)


async def create_local_user_from_casdoor(
    username: str,
    oauth_token: CasdoorOAuthToken,
    *,
    email: str = "",
    uname: str = "",
    ip: str | None = None,
    ua: str | None = None,
    session: AsyncSession | None = None,
) -> LocalUserResult | None:
    """从 Casdoor 用户信息创建本地用户。

    流程：
    1. 优先按邮箱查重：TUserDetail.email 已存在则直接返回已有用户
    2. 按 username 查重：冲突则尝试 bili_ + username，仍冲突则 bili_ + uuid
    3. 创建用户（TUserInfo + TUserDetail + TUserLevel + TUserVip）
    4. 将 access_token 写入 TUserInfo.pwd（便于后续直接调用 Casdoor API）
    5. 记录注册 IP 信息

    Args:
        username: Casdoor 用户名（即本地 TUserInfo.user_name）
        oauth_token: CasdoorOAuthToken 实例
        email: Casdoor 用户邮箱（写入 TUserDetail.email，用于去重）
        uname: Casdoor 用户昵称（写入 TUserDetail.uname）
        ip: 客户端 IP（可选，用于记录注册 IP）
        ua: 客户端 User-Agent（可选）
        session: 可选数据库会话，传入后整个流程共享同一事务

    Returns:
        LocalUserResult 实例，失败返回 None
    """
    # 1. 优先按邮箱查重：邮箱已存在说明该 Casdoor 用户已创建过本地账号
    if email:

        async def _check_email(s: AsyncSession):
            result = await s.exec(
                select(PptrUserDetail).where(PptrUserDetail.email == email)
            )
            return result.first()

        if session is not None:
            existing_detail = await _check_email(session)
        else:
            async with new_pptr_session() as s:
                existing_detail = await _check_email(s)

        if existing_detail:
            existing = await PptrUserService.get_user_profile(
                uid=existing_detail.mid, session=session
            )
            if existing:
                info = existing[0]
                logger.info(
                    f"[Casdoor] 邮箱 {email} 已存在，返回已有用户 uid={info.uid}"
                )
                return LocalUserResult(
                    uid=info.uid,
                    user_name=info.user_name or username,
                    role=info.role or "0",
                )

    # 2. 按 username 查重，冲突则自动分配
    final_username = username
    existing = await PptrUserService.get_user_profile(
        user_name=final_username, session=session
    )
    if existing:
        # 尝试 bili_ + username
        final_username = f"bili_{username}"
        existing = await PptrUserService.get_user_profile(
            user_name=final_username, session=session
        )
        if existing:
            # 最终回退：bili_ + uuid
            final_username = f"bili_{_uuid.uuid4().hex[:12]}"
            logger.info(
                f"[Casdoor] username {username} 和 bili_{username} 均冲突，"
                f"回退为 {final_username}"
            )
        else:
            logger.info(
                f"[Casdoor] username {username} 冲突，使用 bili_{username}"
            )

    # 3. 创建用户（仅将 access_token 写入 TUserInfo.pwd，便于后续直接调用 Casdoor API）
    uid, created = await PptrUserService.create_user(
        user_name=final_username,
        pwd=oauth_token.access_token,
        uname=uname or username,
        email=email,
        session=session,
    )
    if not created or not uid:
        logger.error(f"[Casdoor] 创建用户 {final_username} 失败")
        return None

    # 4. 记录注册 IP 信息（与用户创建共享同一 session）
    if ip:
        await record_login_activity(
            uid,
            ip=ip,
            ua=ua,
            act_info="reg",
            session=session,
        )

    logger.info(f"[Casdoor] 成功创建本地用户: {final_username}, uid: {uid}")
    return LocalUserResult(
        uid=uid,
        user_name=final_username,
    )


async def record_login_activity(
    uid: int,
    *,
    ip: str | None = None,
    ua: str | None = None,
    act_info: str = "login_succ",
    session: AsyncSession | None = None,
) -> None:
    """记录用户登录活动（TUserActInfoLog）。

    Args:
        uid: 本地用户 uid
        ip: 客户端 IP
        ua: 客户端 User-Agent
        act_info: 活动类型（login_succ / reg）
        session: 可选数据库会话，传入后共享同一事务
    """
    async def _run(s: AsyncSession) -> None:
        log = PptrUserActInfoLog(
            mid=uid,
            ip=ip or "",
            ua=ua or "",
            headers={},
            act_info=act_info,
        )
        s.add(log)
        await s.commit()

    if session is not None:
        await _run(session)
    else:
        async with new_pptr_session() as s:
            await _run(s)