"""空间行为时间线服务（计划书 §3.3 / §3.3.1）。

我的主页与 TA 主页**共用**同一接口，按访问者身份做隐私过滤：

- `viewer == target`（我的主页）→ 不过滤，返回全部三类条目；
- `viewer != target` 或未登录（TA 主页）→ 按隐私开关**整类过滤**：
  关注条目受 ``show_follow_list``、点赞受 ``show_like_list``、收藏按**逐夹 `is_public`**，
  关闭的类型不出现在结果里（不是返回空对象）。

数据源三张表都按「操作人 mid + created_at 倒序」命中既有索引，各自取窗口后
在内存合并排序。**反映「当前有效状态」**（取消点赞 / 取关 / 取消收藏的明细行被
物理删除，行存在即有效），并做两处显式处理：

1. ``msg_user_follow`` 拉黑是把同一行原地翻转成 ``BLOCKED``（保留原 ``created_at``），
   必须过滤 ``status = FOLLOWING``，否则会把「被拉黑的人」当「已关注的人」展示；
2. 动态软删不会级联删赞 / 收藏明细，时间线必须 JOIN ``TMoment`` 校验
   ``deletedAt IS NULL AND auditStatus = 'NORMAL'``，避免指向已删动态的残留行。

收藏明细唯一键是 ``(bizType, bizId, folderId)``，同一资源可进多个夹，需按
``(bizType, bizId)`` 去重，否则一条动态刷出多条收藏记录。

**2.59.0 卡片化（内层资源卡）**：点赞 / 收藏不再只保留 dynamic——``bizType`` 从
``WHERE`` 移到 ``LEFT JOIN ... ON`` 条件里（只有 dynamic 才要求动态未删且审核 normal），
非 dynamic 资源同样成条。出参新增 ``target_resource``：dynamic 内联**完整动态**
（``MomentFeedService.get_details_batch`` 批量，无 N+1），非 dynamic 带
``name/cover/jump_url``（lottery 走批量 RPC；RPA 是逐条 RPC，本期不回查，
前端渲染无标题 / 无封面的占位附加卡）。
"""

from __future__ import annotations

from datetime import datetime

from bili_common.models import InteractionBizTypeEnum
from loguru import logger
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col

from app.models.db import TFavoriteFolder, TMoment, TResourceFavorite, TResourceLike
from app.models.db.follow_tbl import UserFollow
from app.models.enums import FollowStatusEnum, ResourceAuditStatusEnum
from app.models.schemas.moment import MomentDetailResp
from app.models.schemas.space import (
    SpaceTimelineItem,
    SpaceTimelineResource,
    SpaceTimelineResp,
)
from app.services.common.indexed import Indexed, ResourceDisplay
from app.services.infrastructure.lottery_rpc import get_lottery_rpc_client
from app.services.moment.moment_feed import MomentFeedService
from app.services.user.account import PptrUser
from app.services.user.space.attributes import AttributeKey
from app.services.user.space.timeline import TimelineEntry
from app.services.user.space_privacy import SpacePrivacyService

DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE = 50
#: 收藏明细按 bizId 去重需少量超取，缓冲倍数（去重后仍有足够条目排序取页）
_FAV_BUFFER = 3


def _parse_cursor(cursor: str | None) -> datetime | None:
    """游标 ISO 时间 → datetime（非法 / 空返回 None = 第一页）。"""
    if not cursor:
        return None
    try:
        return datetime.fromisoformat(cursor)
    except (TypeError, ValueError):
        return None


class SpaceTimelineService:
    """空间行为时间线（静态方法集合）。"""

    @staticmethod
    async def list(
        session: AsyncSession,
        target_mid: int,
        viewer_mid: int | None,
        cursor: str | None = None,
        page_size: int = DEFAULT_PAGE_SIZE,
    ) -> SpaceTimelineResp:
        """按 `acted_at` 倒序聚合关注 / 点赞 / 收藏三类条目，并按 viewer 过滤。

        `viewer_mid` 缺失（未登录）按 TA 主页口径走公开过滤；游标推进用
        「本页最后一条的 acted_at」，逐页重扫当前时间窗口，适合个人空间的时间线。
        """
        page_size = max(1, min(int(page_size), MAX_PAGE_SIZE))
        cursor_dt = _parse_cursor(cursor)

        # 私域过滤：可见性判定统一交给 SpaceVisibility（本人恒可见；他人按属性开关，
        # 未登录访客按「非本人」口径）。三类条目都是 TIMELINE_ENTRY 场景的属性。
        visibility = await SpacePrivacyService.visibility_for(
            session, int(target_mid), viewer_mid
        )
        # 时间线总开关（2026-10-08）：本人恒可见；他人开关关闭 → 整条时间线不返回
        if not visibility.can_view(AttributeKey.TIMELINE):
            return SpaceTimelineResp(items=[], has_more=False, cursor=None)
        show_follow = visibility.can_view(AttributeKey.FOLLOW_LIST)
        show_like = visibility.can_view(AttributeKey.LIKE_LIST)

        entries: list[TimelineEntry] = []

        if show_follow:
            stmt = (
                select(
                    col(UserFollow.target_mid).label("target_mid"),
                    col(UserFollow.created_at).label("acted_at"),
                )
                .where(
                    col(UserFollow.mid) == int(target_mid),
                    col(UserFollow.status) == FollowStatusEnum.FOLLOWING,
                )
                .order_by(col(UserFollow.created_at).desc())
            )
            if cursor_dt is not None:
                stmt = stmt.where(col(UserFollow.created_at) < cursor_dt)
            rows = (await session.exec(stmt.limit(page_size))).all()
            entries.extend(
                TimelineEntry.follow(int(r.target_mid), r.acted_at) for r in rows
            )

        if show_like:
            stmt = (
                select(
                    col(TResourceLike.bizType).label("biz_type"),
                    col(TResourceLike.bizId).label("biz_id"),
                    col(TMoment.mid).label("author_mid"),
                    col(TMoment.contentText).label("content"),
                    col(TResourceLike.created_at).label("acted_at"),
                )
                .outerjoin(
                    TMoment,
                    and_(
                        col(TMoment.dynId) == col(TResourceLike.bizId),
                        col(TMoment.deletedAt).is_(None),
                        col(TMoment.auditStatus) == ResourceAuditStatusEnum.NORMAL,
                    ),
                )
                .where(col(TResourceLike.mid) == int(target_mid))
                .order_by(col(TResourceLike.created_at).desc())
            )
            if cursor_dt is not None:
                stmt = stmt.where(col(TResourceLike.created_at) < cursor_dt)
            rows = (await session.exec(stmt.limit(page_size))).all()
            for r in rows:
                entries.append(
                    TimelineEntry.resource(
                        act_type="like",
                        biz_type=r.biz_type,
                        biz_id=int(r.biz_id),
                        author_mid=r.author_mid,
                        content=r.content,
                        acted_at=r.acted_at,
                    )
                )

        # 收藏条目（2026-10-08 改版）：取消全局开关 —— **恒查询**；
        # 本人看全部收藏，他人只看**公开夹**（`TFavoriteFolder.is_public`）里的收藏。
        stmt = (
            select(
                col(TResourceFavorite.bizType).label("biz_type"),
                col(TResourceFavorite.bizId).label("biz_id"),
                col(TMoment.mid).label("author_mid"),
                col(TMoment.contentText).label("content"),
                col(TFavoriteFolder.name).label("folder_name"),
                col(TResourceFavorite.created_at).label("acted_at"),
            )
            .outerjoin(
                TMoment,
                and_(
                    col(TMoment.dynId) == col(TResourceFavorite.bizId),
                    col(TMoment.deletedAt).is_(None),
                    col(TMoment.auditStatus) == ResourceAuditStatusEnum.NORMAL,
                ),
            )
            .join(
                TFavoriteFolder,
                col(TFavoriteFolder.folder_id) == col(TResourceFavorite.folderId),
            )
            .where(col(TResourceFavorite.mid) == int(target_mid))
            .order_by(col(TResourceFavorite.created_at).desc())
        )
        if not visibility.is_owner:
            stmt = stmt.where(col(TFavoriteFolder.is_public).is_(True))
        if cursor_dt is not None:
            stmt = stmt.where(col(TResourceFavorite.created_at) < cursor_dt)
        rows = (await session.exec(stmt.limit(page_size * _FAV_BUFFER))).all()
        seen_biz: set[tuple[str, int]] = set()
        for r in rows:
            biz_id = int(r.biz_id)
            biz_type = InteractionBizTypeEnum(r.biz_type)
            dedup_key = (biz_type.to_text(), biz_id)
            if dedup_key in seen_biz:
                continue
            seen_biz.add(dedup_key)
            entry = TimelineEntry.resource(
                act_type="favorite",
                biz_type=r.biz_type,
                biz_id=biz_id,
                author_mid=r.author_mid,
                content=r.content,
                acted_at=r.acted_at,
            )
            entries.append(entry.with_folder(r.folder_name))

        entries.sort(key=lambda e: e.acted_at, reverse=True)
        page = entries[:page_size]
        has_more = len(entries) > page_size
        next_cursor = entries[page_size - 1].acted_at.isoformat() if has_more else None

        if not page:
            return SpaceTimelineResp(items=[], has_more=False, cursor=None)

        # 批量回查用户简写（被关注者 + 动态作者），pptr 弱依赖
        # 关注条目必有target_mid；资源类条目仅 dynamic 有作者，非 dynamic 为 None
        mid_set = {e.target_mid for e in page if e.target_mid is not None}
        briefs = await PptrUser.get_many(list(mid_set))

        # 2.59.0：内层资源卡 —— dynamic 批量内联完整动态（非 dynamic 走资源信息回查）
        moments = await _load_moments(session, page, viewer_mid)
        lotteries = await _load_lottery_details(page)

        items = []
        for e in page:
            brief = briefs.of_mid(e.target_mid) if e.target_mid is not None else None
            items.append(
                SpaceTimelineItem(
                    act_type=e.act_type,
                    target_mid=e.target_mid,
                    target_name=brief.name if brief else None,
                    target_face=brief.face if brief else None,
                    target_text=e.target_text,
                    target_dyn_id=e.target_dyn_id,
                    target_folder_name=e.target_folder_name,
                    acted_at=e.acted_at,
                    target_resource=_build_resource(e, moments, lotteries),
                )
            )
        return SpaceTimelineResp(items=items, has_more=has_more, cursor=next_cursor)


async def _load_moments(
    session: AsyncSession, page: list[TimelineEntry], viewer_mid: int | None
) -> Indexed[MomentDetailResp]:
    """dynamic 条目批量内联完整动态（一次批量详情，无 N+1）。

    页大小 ≤ ``_DETAIL_BATCH_LIMIT``（20），与批量详情接口上限一致；
    弱依赖：详情装配异常时降级为「只有资源定位、没有 moment」，前端仍能渲染占位卡。
    """
    dyn_ids = [int(e.biz_id) for e in page if e.is_dynamic and e.biz_id is not None]
    if not dyn_ids:
        return Indexed.empty()
    try:
        details = await MomentFeedService.get_details_batch(
            session, dyn_ids, viewer_mid=viewer_mid
        )
    except Exception as e:
        logger.warning("[SpaceTimelineService] 批量动态详情失败，降级为占位卡: {}", e)
        return Indexed.empty()
    return Indexed.of((int(d.dynId), d) for d in details)


async def _load_lottery_details(page: list[TimelineEntry]) -> Indexed[ResourceDisplay]:
    """lottery 资源批量回查 ``{name, cover, jumpUrl}``（弱依赖，失败返回空 `Indexed`）。

    只批量处理 lottery：RPA 类资源回查是**逐条 RPC**（`RpaRpcClient.get_resource_detail`），
    一页最多 20 条会造成 N 次 RPC 抖动，因此 RPA 等类型先返回空展示信息，
    前端渲染无标题 / 无封面的占位附加卡（点击仍可跳转）。
    """
    lottery_ids = [int(e.biz_id) for e in page if e.is_lottery and e.biz_id is not None]
    if not lottery_ids:
        return Indexed.empty()
    client = await get_lottery_rpc_client()
    # RPC 客户端已返回值对象（边界转换点在其内部）
    return await client.get_lottery_details(lottery_ids)


def _build_resource(
    entry: TimelineEntry,
    moments: Indexed[MomentDetailResp],
    lotteries: Indexed[ResourceDisplay],
) -> SpaceTimelineResource | None:
    """内存条目 → 内层资源卡（follow 条目无资源，返回 None）。

    dynamic：带完整 `moment`（缺失时仍返回资源定位，前端降级为占位卡）；
    非 dynamic：带 `name` / `cover` / `jump_url`（回查不到为 None）。
    """
    biz_type = entry.biz_type
    if biz_type is None or entry.biz_id is None:
        return None
    biz_id = int(entry.biz_id)
    if entry.is_dynamic:
        return SpaceTimelineResource(
            biz_type=biz_type.to_text(),
            biz_id=biz_id,
            moment=moments.value_of(biz_id),
        )
    # 非 dynamic：只有 lottery 会批量回查展示信息，其余（RPA 类）保持占位卡
    detail = lotteries.value_of(biz_id) if entry.is_lottery else None
    return SpaceTimelineResource(
        biz_type=biz_type.to_text(),
        biz_id=biz_id,
        name=detail.name if detail else None,
        cover=detail.cover if detail else None,
        jump_url=detail.jump_url if detail else None,
    )


__all__ = ["SpaceTimelineService"]
