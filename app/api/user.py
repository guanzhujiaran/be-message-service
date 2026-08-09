"""管理端用户信息查询（/api/v1/message/admin/user）。

审核场景下，评论 / 私信的作者只带 `mid`，但管理端需要直接看到
「具体用户名」并在悬浮时展示用户详情。用户主数据只有一份（在 pptr 的 Postgres），
本接口直连该库**只读**地按 mid 批量回查，不再依赖本地快照。
"""

from fastapi import APIRouter, Depends, HTTPException, Query
from typing import Annotated

from app.dependencies import MsgAdminUser
from app.models import StandardResponse
from app.models.schemas import CommentUserBrief
from app.services.pptr_user import PptrUserService
from bili_common.models import PptrUserSearchItem, PptrUserSearchResult, UserSearchParams

router = APIRouter(prefix="/api/v1/message/admin/user", tags=["message-admin-user"])


def parse_user_search_params(
    keyword: str = Query(
        "", description="搜索关键字：昵称 / 注册名 / mid / 邮箱"
    ),
    offset: int = Query(0, ge=0, description="分页偏移量（游标），从 0 开始"),
    limit: int = Query(20, ge=1, description="单页条数，默认 20，最大 100"),
) -> UserSearchParams:
    """把 query 参数解析为 `UserSearchParams`。

    注意：不直接用 `Depends(UserSearchParams)`（SQLModel 模型作为 FastAPI query
    依赖会触发 OpenAPI schema 生成 bug），而是用 `Query` 参数解析后构造实例。
    """
    return UserSearchParams(keyword=keyword, offset=offset, limit=limit)


@router.get(
    "/batch",
    response_model=StandardResponse[list[CommentUserBrief]],
    summary="批量查询用户信息（按 mid）",
)
async def batch_user_info(
    user: MsgAdminUser,
    mids: Annotated[list[int], Query(description="要查询的用户 mid 列表，可重复")] = [],
) -> StandardResponse[list[CommentUserBrief]]:
    """按 mid 批量回查用户展示信息（昵称 / 头像 / 等级 / 大会员 / 性别 / 签名）。

    用于审核列表里把作者 `mid` 渲染成具体用户名，并支持悬浮查看详情。
    数据从 pptr Postgres 只读取回；mid 不存在（或已软删）不会报错，仅不出现在返回中。
    """
    data = await PptrUserService.get_many(mids)
    return StandardResponse(data=list(data.values()))


@router.get(
    "/search",
    response_model=StandardResponse[PptrUserSearchResult],
    summary="管理端用户搜索（按昵称 / 注册名 / mid / 邮箱）",
)
async def search_users(
    user: MsgAdminUser,
    params: Annotated[UserSearchParams, Depends(parse_user_search_params)],
) -> StandardResponse[PptrUserSearchResult]:
    """按关键字搜索 pptr 用户（管理端，仅系统管理员 root 可调用）。

    请求参数（`keyword` / `offset` / `limit`）与响应模型 `PptrUserSearchResult`
    均来自 `bili_common.models`，统一管理。

    返回 `StandardResponse`（`code=0` 表示成功，与系统其余接口一致），其 `data`
    为 `PptrUserSearchResult`：`items` 为当前页命中列表，`total` 为满足条件的总命中数
    （用于分页器展示）。分页采用 `offset + limit`，滚动加载时累加 `offset` 即可拉取下一批。
    精确 mid 命中优先于模糊匹配。
    """
    if not getattr(user, "is_root", False):
        raise HTTPException(status_code=403, detail="仅系统管理员(root)可搜索用户")
    items, has_more = await PptrUserService.search_users(
        params.keyword, offset=params.offset, limit=params.limit
    )
    return StandardResponse(data=PptrUserSearchResult(items=items, has_more=has_more))


__all__ = ["router"]
