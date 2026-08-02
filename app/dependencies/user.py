"""用户相关的 FastAPI 依赖（Depends）。

用户信息完全来自上游 nodejs-pptr 代理在转发请求时注入的 x-bili-* 请求头
（网关侧已清除客户端伪造值并重写为可信登录态），微服务间完全互信，
不做令牌 / JWT 校验：以 x-bili-mid 是否非空判断登录态。
"""

from typing import Annotated
from urllib.parse import unquote

from fastapi import Depends, Request

from bili_common.models.depends import AuthInfo

# 与 RPA-Browser / nodejs-pptr ProxyEndPort.setUserHeaders 注入的 x-bili-* 头一一对应
_HEADER_MAP = {
    "x-bili-mid": "mid",
    "x-bili-user-name": "user_name",
    "x-bili-uname": "uname",
    "x-bili-level": "level",
    "x-bili-role": "role",
    "x-bili-sign": "sign",
    "x-bili-sex": "sex",
    "x-bili-email": "email",
    "x-bili-vip-status": "vip_status",
    "x-bili-vip-type": "vip_type",
}


def get_current_user(request: Request) -> AuthInfo | None:
    """从 x-bili-* 请求头还原推送发起方用户信息（FastAPI 依赖）。

    上游代理（nodejs-pptr ProxyEndPort）会用 URL-encode 写入这些头，
    这里统一解码后构造 AuthInfo（统一认证模型）；无相关头时返回 None（匿名推送）。
    """
    data: dict = {}
    for header, field in _HEADER_MAP.items():
        val = request.headers.get(header)
        if val:
            data[field] = unquote(val)
    if not data:
        return None
    return AuthInfo(**data)


# 路由函数签名中直接使用的依赖注解类型
CurrentUser = Annotated[AuthInfo | None, Depends(get_current_user)]
