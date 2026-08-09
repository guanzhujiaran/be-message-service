"""Casdoor OAuth 相关数据模型。

不映射数据库表（table=False），仅用于类型标注和 API 交互。
"""

from pydantic import ConfigDict
from sqlmodel import SQLModel, Field


class CasdoorOAuthToken(SQLModel, table=False):
    """Casdoor OAuth2 token 响应（grant_type=authorization_code）。"""

    access_token: str
    refresh_token: str = ""
    token_type: str = "Bearer"
    expires_in: int = 0
    scope: str = ""


class CasdoorJwtUser(SQLModel, table=False):
    """Casdoor access_token JWT 解析出的用户信息。"""

    name: str = ""
    display_name: str = Field(default="", alias="displayName")
    email: str = ""
    avatar: str = ""

    model_config = ConfigDict(populate_by_name=True, extra="ignore")


class CasdoorApiUser(SQLModel, table=False):
    """Casdoor /api/get-user 返回的用户信息。"""

    owner: str = ""
    name: str = ""
    display_name: str = Field(default="", alias="displayName")
    email: str = ""
    avatar: str = ""

    model_config = ConfigDict(populate_by_name=True, extra="ignore")


class LocalUserResult(SQLModel, table=False):
    """create_local_user_from_casdoor 返回的本地用户信息。"""

    uid: int
    user_name: str
    level: str = "0"
    role: str = "0"