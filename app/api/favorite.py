"""动态收藏夹路由（收藏夹系统，P4-T10）。

路由前缀 `/api/v1/favorite`，全部需登录（`RequiredUser`）。
`folder_id` / `dyn_id` 均为雪花 ID，传输用字符串，路由层 `int()` 转换。
"""

from typing import Annotated

from fastapi import APIRouter, Query
from sqlmodel import col, select

from app.core.database import SessionDep
from app.dependencies import RequiredUser
from app.models import StandardResponse
from app.models.str_int import StrInt
from app.models.enums import InteractionBizTypeEnum
from app.models.schemas.favorite import (
    FavoriteAddReq,
    FavoriteAddResp,
    FavoriteDynFoldersResp,
    FavoriteFolderCreateReq,
    FavoriteFolderDeleteReq,
    FavoriteFolderResp,
    FavoriteFolderUpdateReq,
    FavoriteItemListResp,
    FavoriteListItem,
    FavoriteListReq,
    FavoriteListResp,
    FavoriteRemoveReq,
    FavoriteSettingReq,
    FavoriteSettingResp,
)
from app.services.moment.interaction import BeMessageInteractionStatService as InteractionStatService
from app.services.interaction_actions import get_biz
from app.services.interaction_actions.folder import FavoriteFolderAction

router = APIRouter(prefix="/api/v1/favorite", tags=["favorite"])


async def _parse_int(value: str | None, field: str) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _resolve_biz(biz_type: InteractionBizTypeEnum, biz_id: str | None, dyn_id: str | None) -> tuple[InteractionBizTypeEnum, int] | None:
    """解析收藏请求的目标资源 (biz_type_enum, biz_id_int)。

    - bizType=dynamic：bizId 与 dynId 任取其一；
    - bizType≠dynamic：必须提供 bizId。
    解析失败返回 None。
    """
    if biz_type == InteractionBizTypeEnum.DYNAMIC:
        rid = biz_id if biz_id is not None else dyn_id
        if rid is None:
            return None
        return InteractionBizTypeEnum.DYNAMIC, int(rid)
    if biz_id is None:
        return None
    return biz_type, int(biz_id)


async def _get_favorite_count(session, biz_type: InteractionBizTypeEnum, biz_id: int) -> int:
    """读取某资源收藏数（动态与非动态统一走 TInteractionStat）。"""
    counts = await InteractionStatService.batch_get_counts(session, biz_type, [biz_id])
    return counts.get(biz_id, {}).get("favoriteCount", 0)


# ==================== 收藏夹 CRUD ====================


@router.post("/folder/create", response_model=StandardResponse[FavoriteFolderResp], summary="创建收藏夹")
async def create_folder(
    session: SessionDep,
    user: RequiredUser,
    req: FavoriteFolderCreateReq,
) -> StandardResponse[FavoriteFolderResp]:
    try:
        folder_id, cover_audit_status = await FavoriteFolderAction(session, user.mid).create(
            req.name, req.description, req.coverUrl
        )
    except ValueError as e:
        # 封面 URL 下载校验失败等（复用头像校验：http/https、1s 内下载、≤1MB、image/*）
        return StandardResponse(code=422, msg=str(e))
    return StandardResponse(
        data=FavoriteFolderResp(
            folderId=str(folder_id),
            name=req.name.strip() or "未命名收藏夹",
            description=req.description,
            # 封面先审后发：未审核通过不对外展示（coverAuditStatus=pending 时前端展示「封面待审核」）
            coverUrl=None,
            isDefault=False,
            favoriteCount=0,
            coverAuditStatus=cover_audit_status,
        )
    )


@router.post("/folder/update", response_model=StandardResponse, summary="更新收藏夹")
async def update_folder(
    session: SessionDep,
    user: RequiredUser,
    req: FavoriteFolderUpdateReq,
) -> StandardResponse:
    folder_id = await _parse_int(req.folderId, "folderId")
    if folder_id is None:
        return StandardResponse(code=400, msg="folderId 不合法")
    try:
        await FavoriteFolderAction(session, user.mid).update(
            folder_id,
            req.name,
            req.description,
            req.coverUrl,
        )
    except ValueError as e:
        if str(e) == "收藏夹不存在":
            return StandardResponse(code=400, msg=str(e))
        # 封面 URL 下载校验失败（422）
        return StandardResponse(code=422, msg=str(e))
    return StandardResponse(data=None)


@router.post("/folder/delete", response_model=StandardResponse, summary="删除收藏夹")
async def delete_folder(
    session: SessionDep,
    user: RequiredUser,
    req: FavoriteFolderDeleteReq,
) -> StandardResponse:
    folder_id = await _parse_int(req.folderId, "folderId")
    if folder_id is None:
        return StandardResponse(code=400, msg="folderId 不合法")
    try:
        await FavoriteFolderAction(session, user.mid).delete(folder_id)
    except ValueError as e:
        return StandardResponse(code=400, msg=str(e))
    return StandardResponse(data=None)


@router.get("/folder/list", response_model=StandardResponse[list[FavoriteFolderResp]], summary="我的收藏夹列表")
async def list_folders(
    session: SessionDep,
    user: RequiredUser,
) -> StandardResponse[list[FavoriteFolderResp]]:
    # 每用户自动 get_or_create 默认收藏夹，保证新用户无需先建夹即可收藏（计划书决策 15）
    folder_action = FavoriteFolderAction(session, user.mid)
    await folder_action.ensure_default()
    folders = await folder_action.list_folders()
    return StandardResponse(data=[FavoriteFolderResp(**f) for f in folders])


# ==================== 收藏 / 取消 ====================


@router.post("/add", response_model=StandardResponse[FavoriteAddResp], summary="收藏资源到收藏夹")
async def add_favorite(
    session: SessionDep,
    user: RequiredUser,
    req: FavoriteAddReq,
) -> StandardResponse[FavoriteAddResp]:
    biz_type = req.bizType
    resolved = _resolve_biz(biz_type, req.bizId, req.dynId)
    if resolved is None:
        return StandardResponse(code=400, msg="bizId/dynId 不合法")
    biz_type, biz_id = resolved
    try:
        # 2.48.0：以资源为主体，取资源实例调用 favorite()
        biz = get_biz(biz_type, session, biz_id, user.mid)
        favorited, folder_id = await biz.favorite(action="add", folder_id=req.folderId)
    except ValueError as e:
        return StandardResponse(code=400, msg=str(e))
    count = await _get_favorite_count(session, biz_type, biz_id)
    return StandardResponse(
        data=FavoriteAddResp(
            bizType=biz_type,
            bizId=str(biz_id),
            dynId=req.dynId,
            folderId=str(folder_id),
            favorited=favorited,
            favoriteCount=count,
        )
    )


@router.post("/remove", response_model=StandardResponse[FavoriteAddResp], summary="从收藏夹取消收藏")
async def remove_favorite(
    session: SessionDep,
    user: RequiredUser,
    req: FavoriteRemoveReq,
) -> StandardResponse[FavoriteAddResp]:
    biz_type = req.bizType
    resolved = _resolve_biz(biz_type, req.bizId, req.dynId)
    folder_id = await _parse_int(req.folderId, "folderId")
    if resolved is None or folder_id is None:
        return StandardResponse(code=400, msg="bizId/dynId/folderId 不合法")
    biz_type, biz_id = resolved
    biz = get_biz(biz_type, session, biz_id, user.mid)
    removed, _ = await biz.favorite(action="remove", folder_id=folder_id)
    count = await _get_favorite_count(session, biz_type, biz_id)
    return StandardResponse(
        data=FavoriteAddResp(
            bizType=biz_type,
            bizId=str(biz_id),
            dynId=req.dynId,
            folderId=req.folderId,
            favorited=not removed,
            favoriteCount=count,
        )
    )


@router.get("/list", response_model=StandardResponse[FavoriteListResp], summary="某收藏夹下的资源列表")
async def list_favorites(
    session: SessionDep,
    user: RequiredUser,
    folderId: str = Query(description="收藏夹id（字符串）"),
    bizType: InteractionBizTypeEnum | None = Query(default=None, description="资源类型过滤（缺省返回全部；InteractionBizTypeEnum 值）"),
    page: int = Query(default=1, ge=1),
    pageSize: int = Query(default=20, ge=1, le=50),
) -> StandardResponse[FavoriteListResp]:
    folder_id = await _parse_int(folderId, "folderId")
    if folder_id is None:
        return StandardResponse(code=400, msg="folderId 不合法")
    total, items = await FavoriteFolderAction(session, user.mid).list_items(
        folder_id, page, pageSize, biz_type=bizType
    )
    # 兼容字段：仅当过滤为 dynamic 或不过滤时，把 dynamic 项的 bizId 填到 dynIds
    dyn_ids = [it["bizId"] for it in items if it["bizType"] == InteractionBizTypeEnum.DYNAMIC]
    return StandardResponse(
        data=FavoriteListResp(
            folderId=folderId,
            total=total,
            dynIds=dyn_ids,
            items=[FavoriteListItem(**it) for it in items],
        )
    )


@router.get("/items", response_model=StandardResponse[FavoriteItemListResp], summary="某收藏夹下资源明细（bizType+bizId）")
async def list_favorite_items(
    session: SessionDep,
    user: RequiredUser,
    folderId: str = Query(description="收藏夹id（字符串）"),
    bizType: InteractionBizTypeEnum | None = Query(default=None, description="资源类型过滤（缺省返回全部；InteractionBizTypeEnum 值）"),
    page: int = Query(default=1, ge=1),
    pageSize: int = Query(default=20, ge=1, le=50),
) -> StandardResponse[FavoriteItemListResp]:
    folder_id = await _parse_int(folderId, "folderId")
    if folder_id is None:
        return StandardResponse(code=400, msg="folderId 不合法")
    total, items = await FavoriteFolderAction(session, user.mid).list_items(
        folder_id, page, pageSize, biz_type=bizType
    )
    return StandardResponse(
        data=FavoriteItemListResp(
            folderId=folderId,
            total=total,
            items=[FavoriteListItem(**it) for it in items],
        )
    )


@router.get("/dyn/folders", response_model=StandardResponse[FavoriteDynFoldersResp], summary="某资源被当前用户收藏在哪些收藏夹")
async def dyn_folders(
    session: SessionDep,
    user: RequiredUser,
    bizId: str | None = Query(default=None, description="资源id（字符串）"),
    bizType: InteractionBizTypeEnum = Query(default=InteractionBizTypeEnum.DYNAMIC, description="资源类型（InteractionBizTypeEnum 值）"),
    dynId: str | None = Query(default=None, description="[兼容]动态id（字符串）"),
) -> StandardResponse[FavoriteDynFoldersResp]:
    resolved = _resolve_biz(bizType, bizId, dynId)
    if resolved is None:
        return StandardResponse(code=400, msg="bizId/dynId 不合法")
    biz_type, biz_id = resolved
    folder_ids = await FavoriteFolderAction(session, user.mid).folders_containing(
        biz_type, biz_id
    )
    return StandardResponse(
        data=FavoriteDynFoldersResp(
            bizType=biz_type,
            bizId=str(biz_id),
            dynId=dynId,
            folderIds=folder_ids,
        )
    )


# ==================== 主页收藏可见性设置 ====================


@router.get("/setting", response_model=StandardResponse[FavoriteSettingResp], summary="主页是否显示收藏")
async def get_setting(
    session: SessionDep,
    user: RequiredUser,
) -> StandardResponse[FavoriteSettingResp]:
    show = await FavoriteFolderAction(session, user.mid).get_setting()
    return StandardResponse(data=FavoriteSettingResp(showFavorites=show))


@router.post("/setting", response_model=StandardResponse[FavoriteSettingResp], summary="设置主页是否显示收藏")
async def set_setting(
    session: SessionDep,
    user: RequiredUser,
    req: FavoriteSettingReq,
) -> StandardResponse[FavoriteSettingResp]:
    show = await FavoriteFolderAction(session, user.mid).set_setting(req.showFavorites)
    return StandardResponse(data=FavoriteSettingResp(showFavorites=show))


# ==================== 他人主页公开读（无需登录）====================


@router.get("/user/folders", response_model=StandardResponse[list[FavoriteFolderResp] | None], summary="某用户主页公开的收藏夹列表")
async def public_folders(
    session: SessionDep,
    mid: Annotated[
        StrInt, Query(description="目标用户mid（雪花 ID，StrInt 兼容前端 str 传参）")
    ],
) -> StandardResponse[list[FavoriteFolderResp] | None]:
    folders = await FavoriteFolderAction(session, user.mid).list_public_folders(int(mid))
    if folders is None:
        return StandardResponse(code=403, msg="该用户未公开收藏")
    return StandardResponse(data=[FavoriteFolderResp(**f) for f in folders])


@router.get("/user/dynamics", response_model=StandardResponse[FavoriteListResp | None], summary="某用户某收藏夹下的公开资源")
async def public_dynamics(
    session: SessionDep,
    mid: Annotated[
        StrInt, Query(description="目标用户mid（雪花 ID，StrInt 兼容前端 str 传参）")
    ],
    folderId: str = Query(description="收藏夹id（字符串）"),
    page: int = Query(default=1, ge=1),
    pageSize: int = Query(default=20, ge=1, le=50),
) -> StandardResponse[FavoriteListResp | None]:
    folder_id = await _parse_int(folderId, "folderId")
    if folder_id is None:
        return StandardResponse(code=400, msg="folderId 不合法")
    result = await FavoriteFolderAction(session, user.mid).list_public_items(
        int(mid), folder_id, page, pageSize
    )
    if result is None:
        return StandardResponse(code=403, msg="该收藏夹不公开或不存在")
    total, items = result
    dyn_ids = [it["bizId"] for it in items if it["bizType"] == InteractionBizTypeEnum.DYNAMIC]
    return StandardResponse(
        data=FavoriteListResp(
            folderId=folderId,
            total=total,
            dynIds=dyn_ids,
            items=[FavoriteListItem(**it) for it in items],
        )
    )
