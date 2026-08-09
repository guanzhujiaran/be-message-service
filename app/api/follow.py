"""用户关注 / 拉黑 HTTP 接口（/api/v1/message/follow）。

提供关注 / 取关 / 拉黑 / 解除拉黑、关系查询、关注与粉丝列表、计数等。
关系数据自包含于 `msg_user_follow`，用户展示信息不在本服务冗余，
列表接口仅返回 `mid` 与关系建立时间，前端可按 mid 批量回查 pptr 主数据。
"""

from fastapi import APIRouter, Query

from app.core.database import SessionDep
from app.dependencies import CurrentUser
from app.models import StandardResponse
from app.models.schemas import (
    BlockReq,
    FollowCountResp,
    FollowListResp,
    FollowOpResp,
    FollowRelationResp,
    FollowReq,
)
from app.services.follow import FollowService

router = APIRouter(prefix="/api/v1/message/follow", tags=["message-follow"])


# ==================== 写操作 ====================


@router.post(
    "/do",
    response_model=StandardResponse[FollowOpResp],
    summary="关注用户",
)
async def follow_user(
    session: SessionDep, user: CurrentUser, req: FollowReq
) -> StandardResponse[FollowOpResp]:
    """关注 target_mid。

    - 幂等：已关注则保持 `following`；
    - 若此前拉黑过对方，状态翻转为 `following`（拉黑→关注）；
    - 若对方已拉黑你，则拒绝关注。
    """
    try:
        data = await FollowService.follow(session, user.mid, req.target_mid)
    except ValueError as e:
        return StandardResponse(code=400, msg=str(e))
    return StandardResponse(data=data)


@router.post(
    "/undo",
    response_model=StandardResponse[FollowOpResp],
    summary="取关用户",
)
async def unfollow_user(
    session: SessionDep, user: CurrentUser, req: FollowReq
) -> StandardResponse[FollowOpResp]:
    """取关 target_mid。

    仅删除 `following` 记录；若存在 `blocked` 记录则保留（取关不等于解除拉黑）。
    幂等：未关注时也返回成功。
    """
    try:
        data = await FollowService.unfollow(session, user.mid, req.target_mid)
    except ValueError as e:
        return StandardResponse(code=400, msg=str(e))
    return StandardResponse(data=data)


@router.post(
    "/block",
    response_model=StandardResponse[FollowOpResp],
    summary="拉黑用户",
)
async def block_user(
    session: SessionDep, user: CurrentUser, req: BlockReq
) -> StandardResponse[FollowOpResp]:
    """拉黑 target_mid。

    - 幂等：已拉黑则保持 `blocked`；
    - 若此前关注过对方，状态翻转为 `blocked`（关注→拉黑）；
    - **同时删除对方对自己的 `following` 记录**，使对方不再是自己的粉丝。
    """
    try:
        data = await FollowService.block(session, user.mid, req.target_mid)
    except ValueError as e:
        return StandardResponse(code=400, msg=str(e))
    return StandardResponse(data=data)


@router.post(
    "/unblock",
    response_model=StandardResponse[FollowOpResp],
    summary="解除拉黑",
)
async def unblock_user(
    session: SessionDep, user: CurrentUser, req: BlockReq
) -> StandardResponse[FollowOpResp]:
    """解除拉黑 target_mid。

    仅删除 `blocked` 记录；若存在 `following` 记录则保留。幂等：未拉黑时也返回成功。
    """
    try:
        data = await FollowService.unblock(session, user.mid, req.target_mid)
    except ValueError as e:
        return StandardResponse(code=400, msg=str(e))
    return StandardResponse(data=data)


# ==================== 关系查询 ====================


@router.get(
    "/relation",
    response_model=StandardResponse[FollowRelationResp],
    summary="查询我与某用户的关系",
)
async def get_relation(
    session: SessionDep,
    user: CurrentUser,
    target_mid: int = Query(..., description="目标用户 mid"),
) -> StandardResponse[FollowRelationResp]:
    """查询当前用户与 target_mid 的双向关系。

    返回字段包含：我是否关注对方、对方是否关注我、是否互相关注、我是否拉黑对方、对方是否拉黑我。
    """
    data = await FollowService.get_relation(session, user.mid, target_mid)
    return StandardResponse(data=data)


@router.get(
    "/count",
    response_model=StandardResponse[FollowCountResp],
    summary="查询关注 / 粉丝 / 互相关注数",
)
async def get_counts(
    session: SessionDep, user: CurrentUser
) -> StandardResponse[FollowCountResp]:
    """获取当前用户的关注数、粉丝数、互相关注数。"""
    data = await FollowService.get_counts(session, user.mid)
    return StandardResponse(data=data)


@router.get(
    "/following/list",
    response_model=StandardResponse[FollowListResp],
    summary="我的关注列表",
)
async def list_following(
    session: SessionDep,
    user: CurrentUser,
    page_num: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
) -> StandardResponse[FollowListResp]:
    """我关注的人列表（按关注时间倒序），含 `mutual` 标记是否互相关注。"""
    data = await FollowService.list_following(
        session, user.mid, page_num=page_num, page_size=page_size
    )
    return StandardResponse(data=data)


@router.get(
    "/followers/list",
    response_model=StandardResponse[FollowListResp],
    summary="我的粉丝列表",
)
async def list_followers(
    session: SessionDep,
    user: CurrentUser,
    page_num: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
) -> StandardResponse[FollowListResp]:
    """关注我的人列表（按关注时间倒序），含 `mutual` 标记是否互相关注。"""
    data = await FollowService.list_followers(
        session, user.mid, page_num=page_num, page_size=page_size
    )
    return StandardResponse(data=data)


__all__ = ["router"]
