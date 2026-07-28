"""FastAPI 依赖（Depends）集中存放目录，供各 api 路由复用。"""

from app.dependencies.user import CurrentUser, get_current_user

__all__ = ["CurrentUser", "get_current_user"]
