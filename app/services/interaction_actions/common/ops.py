"""共享操作实现（2.48.0）：供各资源类的方法复用，避免「每资源重复写一遍」。

本模块是把原 `common/` 下各「通用互动动作类」的 `do_execute` 逻辑**原样抽成函数**：
资源类（继承 `BaseBiz`）在实现自己的操作方法时按需调用，逻辑零丢失。

计数分两条链路（与原实现一致，勿混用）：
- **动态**：`MomentStatService.incr_stat / decr_stat`（`TMoment` 计数列），明细 `dynId` 填实际值；
- **非动态通用资源**：`InteractionStatService.incr / decr`（`TInteractionStat`），明细 `dynId=None`。
"""

from sqlalchemy.exc import IntegrityError
from sqlmodel import col, delete, select

from app.models.db import (
    TFavoriteFolder,
    TInteractionStat,
    TMomentDislike,
    TMomentFavorite,
    TMomentLike,
)
from app.models.enums import InteractionBizTypeEnum
from app.services.interaction_actions.base import InteractionActionError
from app.services.moment.interaction import (
    BeMessageInteractionStatService as InteractionStatService,
    InteractionResourceValidator,
)

__all__ = [
    "validate_exists",
    "do_like_generic",
    "do_like_dynamic",
    "do_dislike",
    "do_favorite",
    "do_share",
    "do_repost",
    "do_view",
]


async def validate_exists(session, biz_type: InteractionBizTypeEnum, biz_id: int) -> None:
    """资源存在性校验（注册式校验器；未注册类型默认放行）。"""
    await InteractionResourceValidator.validate(session, biz_type, biz_id)


async def do_like_generic(
    session, biz_type: InteractionBizTypeEnum, biz_id: int, actor_mid: int, up: int = 1
) -> tuple[bool, int]:
    """通用资源点赞 / 取消点赞（幂等；明细 + `TInteractionStat.likeCount`）。"""
    if up not in (1, 2):
        raise InteractionActionError("up 参数不合法（1=点赞, 2=取消点赞）")
    existing = (
        await session.exec(
            select(TMomentLike.pk).where(
                col(TMomentLike.bizType) == biz_type,
                col(TMomentLike.bizId) == biz_id,
                col(TMomentLike.mid) == actor_mid,
            )
        )
    ).first()

    async def _count() -> int:
        counts = await InteractionStatService.batch_get_counts(session, biz_type, [biz_id])
        return counts.get(biz_id, {}).get("likeCount", 0)

    if up == 1:
        if existing is not None:
            return True, await _count()
        session.add(
            TMomentLike(
                bizType=biz_type,
                bizId=biz_id,
                dynId=None,
                mid=actor_mid,
                likeType=1,
            )
        )
        try:
            await session.flush()
        except IntegrityError:
            await session.rollback()
            return True, await _count()
        await InteractionStatService.incr(session, biz_type, biz_id, "likeCount", 1)
        await session.commit()
        return True, await _count()

    if existing is None:
        return False, await _count()
    await session.exec(  # type: ignore[call-overload]
        TMomentLike.__table__.delete().where(col(TMomentLike.pk) == existing)
    )
    await InteractionStatService.decr(session, biz_type, biz_id, "likeCount")
    await session.commit()
    return False, await _count()


async def do_like_dynamic(
    session, biz_id: int, actor_mid: int, up: int = 1
) -> tuple[bool, int]:
    """动态点赞 / 取消点赞（幂等；明细 `dynId` 填实际值 + `MomentStatService` 计数）。"""
    from app.services.moment.moment_stat import MomentStatService

    biz_type = InteractionBizTypeEnum.DYNAMIC
    if up not in (1, 2):
        raise InteractionActionError("up 参数不合法（1=点赞, 2=取消点赞）")
    existing = (
        await session.exec(
            select(TMomentLike.pk).where(
                col(TMomentLike.bizType) == biz_type,
                col(TMomentLike.bizId) == biz_id,
                col(TMomentLike.mid) == actor_mid,
            )
        )
    ).first()

    async def _count() -> int:
        stat = (
            await session.exec(
                select(TInteractionStat.likeCount).where(
                    col(TInteractionStat.bizType) == biz_type,
                    col(TInteractionStat.bizId) == biz_id,
                )
            )
        ).first()
        return stat or 0

    if up == 1:
        if existing is not None:
            return True, await _count()
        session.add(
            TMomentLike(
                bizType=biz_type,
                bizId=biz_id,
                dynId=biz_id,
                mid=actor_mid,
                likeType=1,
            )
        )
        try:
            await session.flush()
        except IntegrityError:
            await session.rollback()
            return True, await _count()
        await MomentStatService.incr_stat(session, biz_id, "likeCount", 1)
        await session.commit()
        return True, await _count()

    if existing is None:
        return False, await _count()
    await session.exec(  # type: ignore[call-overload]
        TMomentLike.__table__.delete().where(col(TMomentLike.pk) == existing)
    )
    await MomentStatService.decr_stat(session, biz_id, "likeCount", floor_zero=True)
    await session.commit()
    return False, await _count()


async def do_dislike(
    session, biz_type: InteractionBizTypeEnum, biz_id: int, actor_mid: int, up: int = 1
) -> tuple[bool, int]:
    """点踩 / 取消点踩（幂等；明细 `TMomentDislike` + `dislikeCount`）。"""
    if up not in (1, 2):
        raise InteractionActionError("up 参数不合法（1=点踩, 2=取消点踩）")
    existing = (
        await session.exec(
            select(TMomentDislike.pk).where(
                col(TMomentDislike.bizType) == biz_type,
                col(TMomentDislike.bizId) == biz_id,
                col(TMomentDislike.mid) == actor_mid,
            )
        )
    ).first()

    async def _count() -> int:
        counts = await InteractionStatService.batch_get_counts(session, biz_type, [biz_id])
        return counts.get(biz_id, {}).get("dislikeCount", 0)

    if up == 1:
        if existing is not None:
            return True, await _count()
        session.add(
            TMomentDislike(
                bizType=biz_type,
                bizId=biz_id,
                dynId=None,
                mid=actor_mid,
            )
        )
        try:
            await session.flush()
        except IntegrityError:
            await session.rollback()
            return True, await _count()
        await InteractionStatService.incr(session, biz_type, biz_id, "dislikeCount", 1)
        await session.commit()
        return True, await _count()

    if existing is None:
        return False, await _count()
    await session.exec(
        delete(TMomentDislike).where(
            col(TMomentDislike.bizType) == biz_type,
            col(TMomentDislike.bizId) == biz_id,
            col(TMomentDislike.mid) == actor_mid,
        )
    )
    await InteractionStatService.decr(session, biz_type, biz_id, "dislikeCount")
    await session.commit()
    return False, await _count()


async def do_favorite(
    session,
    biz_type: InteractionBizTypeEnum,
    biz_id: int,
    actor_mid: int,
    folder_id=None,
    action: str = "add",
) -> tuple[bool, int]:
    """收藏 / 取消收藏（多夹 + 用户去重计数）。返回 (是否变更, 目标 folder_id)。"""
    if action == "add":
        return await _favorite_add(session, biz_type, biz_id, actor_mid, folder_id)
    if action == "remove":
        return await _favorite_remove(session, biz_type, biz_id, actor_mid, folder_id)
    raise InteractionActionError("action 参数不合法（add/remove）")


async def _ensure_default_folder(session, actor_mid: int) -> int:
    """确保默认收藏夹存在，返回其 folder_id（委托收藏夹管理操作类）。"""
    from app.services.interaction_actions.folder import FavoriteFolderAction

    return await FavoriteFolderAction(session, actor_mid).ensure_default()


async def _favorite_add(
    session, biz_type: InteractionBizTypeEnum, biz_id: int, actor_mid: int, folder_id
) -> tuple[bool, int]:
    """收藏资源到指定收藏夹（幂等）；未指定夹时自动使用（创建）默认夹。"""
    if folder_id is None:
        folder_id = await _ensure_default_folder(session, actor_mid)
    else:
        folder_id = int(folder_id)
    folder = (
        await session.exec(
            select(TFavoriteFolder).where(
                col(TFavoriteFolder.folder_id) == folder_id,
                col(TFavoriteFolder.mid) == actor_mid,
            )
        )
    ).one_or_none()
    if folder is None:
        raise InteractionActionError("收藏夹不存在")

    exists = (
        await session.exec(
            select(TMomentFavorite.pk).where(
                col(TMomentFavorite.bizType) == biz_type,
                col(TMomentFavorite.bizId) == biz_id,
                col(TMomentFavorite.folderId) == folder_id,
            )
        )
    ).first()
    if exists is not None:
        await session.commit()
        return False, folder_id

    session.add(
        TMomentFavorite(
            bizType=biz_type,
            bizId=biz_id,
            dynId=None,
            folderId=folder_id,
            mid=actor_mid,
        )
    )
    await session.flush()

    # 用户去重：该用户在其它夹也未收藏过同一资源才计数 +1
    already = (
        await session.exec(
            select(TMomentFavorite.pk).where(
                col(TMomentFavorite.mid) == actor_mid,
                col(TMomentFavorite.bizType) == biz_type,
                col(TMomentFavorite.bizId) == biz_id,
                col(TMomentFavorite.folderId) != folder_id,
            )
        )
    ).first()
    if already is None:
        await InteractionStatService.incr(session, biz_type, biz_id, "favoriteCount", 1)
    await session.commit()
    return True, folder_id


async def _favorite_remove(
    session, biz_type: InteractionBizTypeEnum, biz_id: int, actor_mid: int, folder_id
) -> tuple[bool, int]:
    """从指定收藏夹取消收藏（幂等）。"""
    if folder_id is None:
        raise InteractionActionError("action 参数不合法（add/remove）")
    folder_id = int(folder_id)
    row = (
        await session.exec(
            select(TMomentFavorite).where(
                col(TMomentFavorite.bizType) == biz_type,
                col(TMomentFavorite.bizId) == biz_id,
                col(TMomentFavorite.folderId) == folder_id,
                col(TMomentFavorite.mid) == actor_mid,
            )
        )
    ).first()
    if row is None:
        await session.commit()
        return False, folder_id
    await session.exec(
        delete(TMomentFavorite).where(
            col(TMomentFavorite.bizType) == biz_type,
            col(TMomentFavorite.bizId) == biz_id,
            col(TMomentFavorite.folderId) == folder_id,
            col(TMomentFavorite.mid) == actor_mid,
        )
    )
    # 用户去重：该用户在其它夹也不再收藏同一资源才计数 -1
    other = (
        await session.exec(
            select(TMomentFavorite.pk).where(
                col(TMomentFavorite.mid) == actor_mid,
                col(TMomentFavorite.bizType) == biz_type,
                col(TMomentFavorite.bizId) == biz_id,
                col(TMomentFavorite.folderId) != folder_id,
            )
        )
    ).first()
    if other is None:
        await InteractionStatService.decr(session, biz_type, biz_id, "favoriteCount")
    await session.commit()
    return True, folder_id


async def do_share(session, biz_type: InteractionBizTypeEnum, biz_id: int) -> int:
    """分享上报（`shareCount` +1，行为上报不幂等），返回最新计数。"""
    await InteractionStatService.incr(session, biz_type, biz_id, "shareCount", 1)
    await session.commit()
    stat = (
        await session.exec(
            select(TInteractionStat.shareCount).where(
                col(TInteractionStat.bizType) == biz_type,
                col(TInteractionStat.bizId) == biz_id,
            )
        )
    ).first()
    return stat or 0


async def do_repost(
    session, biz_type: InteractionBizTypeEnum, biz_id: int, attach_to: int | None = None
) -> int:
    """转发 / attach 行为计数（`repostCount` +1，不幂等），返回最新计数。"""
    await InteractionStatService.incr(session, biz_type, biz_id, "repostCount", 1)
    await session.commit()
    stat = (
        await session.exec(
            select(TInteractionStat.repostCount).where(
                col(TInteractionStat.bizType) == biz_type,
                col(TInteractionStat.bizId) == biz_id,
            )
        )
    ).first()
    return stat or 0


async def do_view(
    session, biz_type: InteractionBizTypeEnum, biz_id: int, actor_mid: int
) -> bool:
    """浏览上报（弱依赖计数，跨自然日去重）。返回 True=新计一次 / False=同日重复。"""
    return await InteractionStatService.report_view(
        session, biz_type, biz_id, actor_mid
    )


async def do_comment(
    session,
    biz_type: InteractionBizTypeEnum,
    biz_id: int,
    actor_mid: int,
    *,
    root: int = 0,
    message: str,
    at_mids=None,
    at_name_to_mid=None,
    pictures=None,
    emote_meta=None,
    up_mid: int = 0,
):
    """在本资源下发表评论（委托评论子系统；`reply` / `at` 资源方法统一复用）。

    `root=0` 为一级评论（回复动态 / 通用资源）；`root=biz_id` 为回复某条评论。
    """
    from app.models.schemas import CommentAddReq
    from app.services.comment.comment import CommentService

    req = CommentAddReq(
        oid=str(biz_id),
        type=biz_type,
        root=str(root),
        message=message,
        at_mids=at_mids or [],
        at_name_to_mid=at_name_to_mid,
        pictures=pictures,
        emote_meta=emote_meta,
        up_mid=up_mid or 0,
    )
    return await CommentService.add(session, actor_mid, req)
