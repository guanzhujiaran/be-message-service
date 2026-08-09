"""JWT 令牌服务（用户网关下沉 be-message）。

与 pptr 侧 JwtModule.js 的 createToken 行为保持一致：
- 算法：HS256
- 载荷：{ user_name, uid, level, role }
- 有效期：15 天
- 密钥：与 pptr 侧 common_config.salt.jwt_secret 相同

当网关将所有 /api/v1/user 路径代理到本服务后，
pptr 仍负责 JWT 验签（jwtAuth 中间件），本服务仅负责签发新 token。
"""

import jwt
from datetime import datetime, timedelta, timezone
from app.core.config import settings


def create_token(
    *,
    user_name: str,
    uid: int | str,
    level: str = "0",
    role: str = "0",
) -> str:
    """签发 JWT 令牌，与 pptr JwtModule.createToken 载荷对齐。

    Args:
        user_name: 登录用户名
        uid: 用户 ID
        level: 用户等级，默认 "0"
        role: 用户角色，默认 "0"

    Returns:
        JWT 字符串
    """
    payload = {
        "user_name": user_name,
        "uid": uid,
        "level": level,
        "role": role,
        "iat": int(datetime.now(timezone.utc).timestamp()),
        "exp": int(
            (datetime.now(timezone.utc) + timedelta(seconds=settings.jwt_expires_seconds)).timestamp()
        ),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def decode_token(token: str) -> dict | None:
    """解码 JWT 令牌（不做验签，仅解析载荷）。

    注：验签仍由 pptr 网关的 jwtAuth 中间件负责。
    本服务仅需读取载荷中的 iat/exp 等字段判断是否需要续期。

    Args:
        token: JWT 字符串

    Returns:
        载荷字典，解析失败返回 None
    """
    try:
        return jwt.decode(
            token,
            settings.jwt_secret,
            algorithms=[settings.jwt_algorithm],
            options={"verify_exp": False},  # 过期 token 仍需能读取 iat 做续期判断
        )
    except jwt.PyJWTError:
        return None


def is_jwt_expired_today(payload: dict | None) -> bool:
    """判断 JWT 是否需要续期（非当天签发的 token 需要刷新）。

    与 pptr UserGatewayProxy.isJwtExpiredToday 逻辑一致。

    Args:
        payload: JWT 载荷字典（含 iat 字段）

    Returns:
        True 表示需要续期
    """
    if not payload or not payload.get("iat"):
        return False
    now = datetime.now(timezone.utc)
    iat_time = datetime.fromtimestamp(payload["iat"], tz=timezone.utc)
    return iat_time.date() < now.date()