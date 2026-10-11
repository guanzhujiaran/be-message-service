"""用户关注 / 拉黑 HTTP 接口（/api/v1/message/follow）。

提供关注 / 取关 / 拉黑 / 解除拉黑、关系查询、关注与粉丝列表、计数等。
关系数据自包含于 `msg_user_follow`，用户展示信息不在本服务冗余，
列表接口仅返回 `mid` 与关系建立时间，前端可按 mid 批量回查 pptr 主数据。

列表接口对标 B 站 `x/relation/followings` / `x/relation/fans`：query 用
`vmid`（被查看者，缺省=自己）+ `pn`（页码）+ `ps`（每页）；
`order` / `order_type`（最近关注 / 最常访问）本期不做。
"""

from typing import Annotated

from bili_common.models import InteractionBizTypeEnum
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
    FollowTagAssignReq,
    FollowTagCreateReq,
    FollowTagResp,
)
from app.models.str_int import StrInt
from app.services.interaction_actions.base_biz import get_biz
from app.services.user.follow import FollowService
from app.services.user.follow_tag import FollowTagService

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
        biz = get_biz(InteractionBizTypeEnum.USER, session, req.target_mid, user.mid)
        data = await biz.follow()
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
        biz = get_biz(InteractionBizTypeEnum.USER, session, req.target_mid, user.mid)
        data = await biz.unfollow()
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
        biz = get_biz(InteractionBizTypeEnum.USER, session, req.target_mid, user.mid)
        data = await biz.block()
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
        biz = get_biz(InteractionBizTypeEnum.USER, session, req.target_mid, user.mid)
        data = await biz.unblock()
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
    # 注意：`StrInt` 必须走 `Annotated[...]`，写成 `x: StrInt = Query(...)` 时
    # FastAPI 会丢掉 BeforeValidator，字符串入参会原样透传（见 str_int 模块说明）
    target_mid: Annotated[StrInt, Query(..., description="目标用户 mid")],
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
    "/followings",
    response_model=StandardResponse[FollowListResp],
    summary="关注列表（缺省 vmid=当前登录用户）",
)
async def list_followings(
    session: SessionDep,
    user: CurrentUser,
    pn: int = Query(default=1, ge=1, description="页码，从 1 开始"),
    ps: int = Query(default=24, ge=1, le=100, description="每页条数"),
    vmid: Annotated[
        StrInt | None, Query(description="被查看的用户 mid，缺省=自己")
    ] = None,
    tagid: int | None = Query(
        default=None,
        description="按分组过滤：-10 特别关注 / 0 默认分组（未分组）/ >0 自建分组；不传=全部",
    ),
) -> StandardResponse[FollowListResp]:
    """TA 关注的人列表（按关注时间倒序），含 `mutual` 标记是否互相关注。

    对标 B 站 `GET /x/relation/followings?vmid=&pn=&ps=`（`order` / `order_type`
    排序参数本期不做，见计划书 §4.1）。

    - `vmid` 缺省=当前登录用户（与改造前行为一致）；
    - `tagid` 仅自己视角有效（分组是私域数据，他人看不到你如何分组）；
    - `vmid` 为他人时受**可见性窗口**约束：最多返回前 5 页（`ps=24` 即最新 120 条），
      超出页码直接返回空列表、`total` 仍为真实总数（见计划书 §4.1）。
    """
    owner_mid = vmid or user.mid
    # 分组过滤只对「看自己」生效：他人视角既无权限也无意义
    effective_tag_id = tagid if owner_mid == user.mid else None
    data = await FollowService.list_following(
        session,
        owner_mid,
        page_num=pn,
        page_size=ps,
        viewer_mid=user.mid,
        tag_id=effective_tag_id,
    )
    return StandardResponse(data=data)


@router.get(
    "/fans",
    response_model=StandardResponse[FollowListResp],
    summary="粉丝列表（缺省 vmid=当前登录用户）",
)
async def list_fans(
    session: SessionDep,
    user: CurrentUser,
    pn: int = Query(default=1, ge=1, description="页码，从 1 开始"),
    ps: int = Query(default=24, ge=1, le=100, description="每页条数"),
    vmid: Annotated[
        StrInt | None, Query(description="被查看的用户 mid，缺省=自己")
    ] = None,
) -> StandardResponse[FollowListResp]:
    """关注 TA 的人列表（按关注时间倒序），含 `mutual` 标记是否互相关注。

    对标 B 站 `GET /x/relation/fans?vmid=&pn=&ps=`；可见性窗口规则与 `followings` 一致：
    看他人最多 5 页，超出返回空列表。
    """
    owner_mid = vmid or user.mid
    data = await FollowService.list_followers(
        session,
        owner_mid,
        page_num=pn,
        page_size=ps,
        viewer_mid=user.mid,
    )
    return StandardResponse(data=data)


# ==================== 关注分组（对标 B 站 x/relation/tags） ====================


@router.get(
    "/tags",
    response_model=StandardResponse[list[FollowTagResp]],
    summary="我的关注分组列表",
)
async def list_follow_tags(
    session: SessionDep, user: CurrentUser
) -> StandardResponse[list[FollowTagResp]]:
    """我的分组列表（特别关注 → 默认分组 → 自建分组）。

    对标 B 站 `GET /x/relation/tags`：`data` 直接是数组，元素形如
    `{"tagid": -10, "name": "特别关注", "count": 5, "tip": "..."}`。
    分组是私域数据，只返回当前登录用户自己的分组。
    """
    return StandardResponse(data=await FollowTagService.list_tags(session, user.mid))


@router.post(
    "/tags",
    response_model=StandardResponse[FollowTagResp],
    summary="新建关注分组",
)
async def create_follow_tag(
    session: SessionDep, user: CurrentUser, req: FollowTagCreateReq
) -> StandardResponse[FollowTagResp]:
    """新建自建分组（名称 1~20 字、不重名、最多 20 个）。"""
    try:
        data = await FollowTagService.create_tag(session, user.mid, req.name, req.tip)
    except ValueError as e:
        return StandardResponse(code=400, msg=str(e))
    return StandardResponse(data=data)


@router.delete(
    "/tags/{tag_id}",
    summary="删除关注分组",
)
async def delete_follow_tag(
    session: SessionDep, user: CurrentUser, tag_id: int
) -> StandardResponse[None]:
    """删除自建分组（连带清空成员关系）；系统分组（-10 / 0）不可删。"""
    try:
        await FollowTagService.delete_tag(session, user.mid, tag_id)
    except ValueError as e:
        return StandardResponse(code=400, msg=str(e))
    return StandardResponse()


@router.get(
    "/tags/assigned",
    response_model=StandardResponse[list[int]],
    summary="查询某用户当前所属的分组 id 列表",
)
async def get_assigned_follow_tags(
    session: SessionDep,
    user: CurrentUser,
    target_mid: Annotated[StrInt, Query(description="目标用户 mid")],
) -> StandardResponse[list[int]]:
    """某用户当前被我放在哪些分组（分组设置弹窗预填用；`0` 默认分组不参与）。"""
    membership = await FollowTagService.membership_of(session, user.mid, target_mid)
    return StandardResponse(data=list(membership.tag_ids))


@router.post(
    "/tags/assign",
    response_model=StandardResponse[list[int]],
    summary="设置某关注对象所属分组（覆盖式）",
)
async def assign_follow_tag(
    session: SessionDep, user: CurrentUser, req: FollowTagAssignReq
) -> StandardResponse[list[int]]:
    """把 target_mid 的分组**覆盖式**设置为 `tag_ids`。

    - 只能操作已关注的人；自建分组必须是自己的；
    - `tag_ids` 传空数组 = 移出全部分组（回到默认分组）；
    - 返回设置后的分组 id 列表。对标 B 站 `POST /x/relation/tags/addUsers`。
    """
    try:
        data = await FollowTagService.assign(
            session, user.mid, req.target_mid, req.tag_ids
        )
    except ValueError as e:
        return StandardResponse(code=400, msg=str(e))
    return StandardResponse(data=data)


__all__ = ["router"]
