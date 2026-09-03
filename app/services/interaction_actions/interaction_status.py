"""互动态查询 / 资源存在性校验（2.56.0 路由层瘦身，计划书 §5.13）。

从 `app/api/moment.py` 上浮为服务层的两块能力，供互动态查询接口与互动写接口共用：

- :func:`resolve_target`：`bizType` + `bizId`
  → 唯一资源定位键 ``(biz_type, biz_id)``（计划书 C18 / C20 / C21；2.56.0 去除 dynId 别名）；
- :class:`InteractionStatusService`：批量互动态装配（`query_status_items` /
  `query_status_item`）与防乱调资源存在性校验（`verify_resources_exist`）。

路由层因此只保留「参数归一 → ``get_biz(...).动作()`` → 装配响应」三步，
不再内联查询编排与按 `bizType` 的分支判断。
"""

import asyncio
from collections.abc import Sequence

from sqlmodel import col, func, select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.db import (
    CommentSubject,
    TMoment,
    TResourceFavorite,
    TResourceLike,
    TResourceReport,
)
from app.models.enums import MomentAuditStatusEnum
from app.models.schemas.interaction import InteractionStatusItem
from app.models.str_int import StrInt
from app.services.infrastructure.rpa_rpc import rpa_rpc_client
from app.services.moment.interaction import (
    BeMessageInteractionStatService as InteractionStatService,
)
from bili_common.models import InteractionBizTypeEnum

__all__ = ["InteractionStatusService", "resolve_target"]


def resolve_target(
    biz_type: InteractionBizTypeEnum,
    biz_id: StrInt | None = None,
) -> tuple[InteractionBizTypeEnum, int]:
    """归一请求中的目标资源为唯一资源定位键 ``(biz_type, biz_id)``。

    `bizId` 为资源唯一标识（2.56.0 起不再接受 `dynId` 别名，动态资源也一律传 `bizId`）；
    缺失 / 非法（非雪花 ID）一律抛 `ValueError`，由路由层统一转 400 错误响应。

    Raises:
        ValueError: 目标资源参数缺失或不合法。
    """
    if biz_id is None:
        raise ValueError("bizId 必填")
    try:
        return biz_type, int(biz_id)
    except (TypeError, ValueError):
        raise ValueError("bizId 不合法") from None


class InteractionStatusService:
    """互动态装配 + 资源存在性校验（纯服务层，不含 HTTP 语义）。"""

    @staticmethod
    async def verify_resources_exist(
        session: AsyncSession, biz_type: InteractionBizTypeEnum, ids: list[int]
    ) -> list[str] | None:
        """批量校验资源存在性（2.23.1 防乱调）。

        - dynamic：本地查 TMoment（deletedAt 非空 / 非 normal 视为不存在）；
        - lottery：批量 RPC 校验（RPC 失败返回 None → 弱依赖降级放行，避免误伤正常用户）；
        - 其余未注册类型：放行。

        Returns:
            缺失的 bizId 字符串列表；全部存在返回 None。
        """
        if biz_type == InteractionBizTypeEnum.DYNAMIC:
            dyn_rows = (
                await session.exec(
                    select(TMoment.dynId).where(
                        col(TMoment.dynId).in_(ids),
                        col(TMoment.deletedAt).is_(None),
                        col(TMoment.auditStatus) == MomentAuditStatusEnum.NORMAL,
                    )
                )
            ).all()
            existing = {int(r) for r in dyn_rows}
        elif biz_type == InteractionBizTypeEnum.LOTTERY:
            from app.services.infrastructure.lottery_rpc import get_lottery_rpc_client

            client = await get_lottery_rpc_client()
            existing = await client.get_existing_lottery_ids(ids)
            if existing is None:
                existing = set(ids)  # RPC 校验不可用：降级放行
        else:
            existing = set(ids)
        missing = [str(_id) for _id in ids if _id not in existing]
        return missing or None

    @staticmethod
    async def query_status_items(
        session: AsyncSession,
        biz_type: InteractionBizTypeEnum,
        ids: Sequence[StrInt],
        mid: StrInt,
    ) -> list[InteractionStatusItem]:
        """装配某类型多个资源的互动状态（计数 / 用户态 / 详情），供 status 两接口共用。"""
        # StrInt 入参可能为 str：服务层统一归一为 int，后续计数 / RPC 一律按 int 处理
        biz_ids = [int(_id) for _id in ids]
        viewer_mid = int(mid)
        # 2.36.0：动态与非动态资源计数统一 TInteractionStat（batch_get_counts 全字段）
        counts = await InteractionStatService.batch_get_counts(session, biz_type, biz_ids)
        like_counts = {b: c["likeCount"] for b, c in counts.items()}
        fav_counts = {b: c["favoriteCount"] for b, c in counts.items()}
        view_counts = {b: c["viewCount"] for b, c in counts.items()}
        comment_counts = {b: c["commentCount"] for b, c in counts.items()}
        repost_counts = {b: c["repostCount"] for b, c in counts.items()}
        for _id in biz_ids:
            like_counts.setdefault(_id, 0)
            fav_counts.setdefault(_id, 0)
        if not InteractionStatService.is_dynamic(biz_type):
            # 评论数：lottery 走评论系统实时计数（rpa_* 无评论功能 → 恒 0）
            if biz_type == InteractionBizTypeEnum.LOTTERY:
                subjects = (
                    await session.exec(
                        select(CommentSubject).where(
                            col(CommentSubject.type) == InteractionBizTypeEnum.LOTTERY,
                            col(CommentSubject.oid).in_(biz_ids),
                        )
                    )
                ).all()
                comment_counts = {s.oid: int(s.all_count) for s in subjects}
            # 转发数：引用该资源生成的动态数（TMoment.bizType/bizRid 可见动态，转发到动态时写入）
            rows = (
                await session.exec(
                    select(col(TMoment.bizRid), func.count())
                    .where(
                        col(TMoment.bizType) == biz_type,
                        col(TMoment.bizRid).in_(biz_ids),
                        col(TMoment.deletedAt).is_(None),
                        col(TMoment.auditStatus) == MomentAuditStatusEnum.NORMAL,
                    )
                    .group_by(col(TMoment.bizRid))
                )
            ).all()
            repost_counts = {int(r): int(c) for r, c in rows}

        # 当前用户点赞态 / 收藏态
        liked_ids = set(
            (
                await session.exec(
                    select(TResourceLike.bizId).where(
                        col(TResourceLike.bizType) == biz_type,
                        col(TResourceLike.bizId).in_(biz_ids),
                        col(TResourceLike.mid) == viewer_mid,
                    )
                )
            ).all()
        )
        faved_ids = set(
            (
                await session.exec(
                    select(TResourceFavorite.bizId).where(
                        col(TResourceFavorite.bizType) == biz_type,
                        col(TResourceFavorite.bizId).in_(biz_ids),
                        col(TResourceFavorite.mid) == viewer_mid,
                    )
                )
            ).all()
        )

        # 非动态资源（RPA 等）详情经 RPC 从资源归属服务获取（弱依赖，失败 detail=None）
        details: dict[int, object] = {}
        if not InteractionStatService.is_dynamic(biz_type):
            results = await asyncio.gather(
                *[
                    rpa_rpc_client.get_resource_detail(biz_type.to_text(), _id)
                    for _id in biz_ids
                ],
                return_exceptions=True,
            )
            for _id, res in zip(biz_ids, results):
                detail = None
                # gather 可能回填 BaseException（如 CancelledError），与 Exception 一并降级
                if not isinstance(res, BaseException) and res is not None:
                    detail = getattr(res, "detail", None)
                details[_id] = detail

        # 2.40.0：被举报人数（去重举报人，同一人多次举报只记一次）
        # 举报 bizType 即业务资源类型（dynamic=1，lottery=2，rpa_*=3~6，comment=7，user=8），按 bizType 聚合
        report_counts: dict[int, tuple[int, int]] = {}
        if biz_ids:
            rp_rows = (
                await session.exec(
                    select(
                        TResourceReport.bizId,
                        func.count(),
                        func.count(func.distinct(TResourceReport.reportMid)),
                    )
                    .where(
                        col(TResourceReport.bizType) == int(biz_type),
                        col(TResourceReport.bizId).in_(biz_ids),
                    )
                    .group_by(col(TResourceReport.bizId))
                )
            ).all()
            report_counts = {int(b): (int(c), int(p)) for b, c, p in rp_rows}

        return [
            InteractionStatusItem(
                bizType=biz_type,
                bizId=str(_id),
                isLike=_id in liked_ids,
                isFavorite=_id in faved_ids,
                likeCount=like_counts.get(_id, 0),
                favoriteCount=fav_counts.get(_id, 0),
                commentCount=comment_counts.get(_id, 0),
                repostCount=repost_counts.get(_id, 0),
                viewCount=view_counts.get(_id, 0),
                reportCount=report_counts.get(_id, (0, 0))[0],
                reportPeopleCount=report_counts.get(_id, (0, 0))[1],
                detail=details.get(_id),
            )
            for _id in biz_ids
        ]

    @classmethod
    async def query_status_item(
        cls,
        session: AsyncSession,
        biz_type: InteractionBizTypeEnum,
        biz_id: int | StrInt,
        mid: StrInt,
    ) -> InteractionStatusItem | None:
        """单资源互动态（detail 接口专用，批量装配的单条便捷封装）。"""
        items = await cls.query_status_items(session, biz_type, [biz_id], mid)
        return items[0] if items else None
