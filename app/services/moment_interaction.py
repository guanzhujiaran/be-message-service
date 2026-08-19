"""动态互动服务（Phase 4）。

覆盖 P4-T3（点赞/取消点赞）、P4-T5（举报）。
浏览计数（P4-T4）无上报入口：由后端在详情接口访问时自动累计
（`moment_stat.MomentStatService.report_view`，见 `app/api/moment_feed.py`）。

设计要点（计划书 §4.2）：

- **点赞幂等**：利用 `TMomentLike(dynId, mid)` 唯一约束做幂等。点赞意图
  （up=1）且明细已存在 → 视为重复点赞，直接返回成功（不 +1、不报错）；
  取消意图（up=2）且明细不存在 → 返回成功（本来就没赞）。避免重试叠加计数。
- **事务双写**：明细 INSERT/DELETE 与 `likeCount` 原子 ±1 在**同一事务**内提交。
- **仅 normal 可点赞**：非 normal / 已软删动态拒绝点赞。
- **举报**：写 `TMomentReport`（reasonType 必须为合法枚举值），**不改变**
  动态 auditStatus。
"""

from loguru import logger
from sqlmodel import col, select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.core.database import new_session
from app.models.db import TMoment, TMomentLike, TMomentReport, TMomentStat
from app.models.enums import (
    EventTypeEnum,
    InteractionBizTypeEnum,
    MomentAuditStatusEnum,
    MomentReportReasonEnum,
    SourceTypeEnum,
)
from bili_common.models.report import ReportBizTypeEnum
from app.models.schemas import EventReportReq
from app.models.schemas.moment import MomentReportReq
from app.services.interaction import (
    BeMessageInteractionStatService as InteractionStatService,
    InteractionResourceValidator,
)
from app.services.moment_stat import MomentStatService


async def _get_visible_normal_dyn(
    session: AsyncSession, moment_id: int
) -> TMoment | None:
    """取动态；非 normal 或已软删返回 None。"""
    dyn = (
        await session.exec(select(TMoment).where(col(TMoment.dynId) == moment_id))
    ).one_or_none()
    if dyn is None or dyn.deletedAt is not None:
        return None
    if dyn.auditStatus != MomentAuditStatusEnum.NORMAL:
        return None
    return dyn


class MomentInteractionService:
    """动态互动服务（静态方法集合，无状态）。"""

    # ==================== 点赞 / 取消点赞（P4-T3）====================

    @staticmethod
    async def thumb(
        session: AsyncSession,
        mid: int,
        biz_type: InteractionBizTypeEnum | str,
        biz_id: int,
        up: int,
        moment_id: int | None = None,
    ) -> tuple[bool, int]:
        """点赞 / 取消点赞（幂等，同一事务内双写；2.17.0 泛化支持多业务资源）。

        Args:
            biz_type: 资源类型（dynamic / lottery / rpa_action / rpa_workflow / rpa_browser）。
            biz_id: 资源 id（动态时 = dynId）。
            up: 1=点赞, 2=取消点赞。
            moment_id: 兼容参数（动态资源时为 dynId，等价 biz_id；已弃用，建议用 biz_id）。

        Returns:
            (is_like, like_count)：操作后当前用户是否已赞、当前点赞数。

        Raises:
            ValueError: 动态不存在 / 非 normal / 已软删（动态）或资源不存在。
        """
        biz_type = InteractionBizTypeEnum.from_text(biz_type)
        target_id = biz_id if moment_id is None else moment_id
        is_dynamic = InteractionStatService.is_dynamic(biz_type)

        if is_dynamic:
            dyn = await _get_visible_normal_dyn(session, target_id)
            if dyn is None:
                raise ValueError("动态不存在或暂不可互动")
        else:
            await InteractionResourceValidator.validate(session, biz_type, target_id)

        existing = (
            await session.exec(
                select(TMomentLike.pk).where(
                    col(TMomentLike.bizType) == biz_type,
                    col(TMomentLike.bizId) == target_id,
                    col(TMomentLike.mid) == mid,
                )
            )
        ).first()

        async def _like_count() -> int:
            if is_dynamic:
                stat = (
                    await session.exec(
                        select(TMomentStat.likeCount).where(
                            col(TMomentStat.dynId) == target_id
                        )
                    )
                ).first()
            else:
                counts = await InteractionStatService.batch_get_counts(
                    session, biz_type, [target_id]
                )
                stat = counts.get(target_id, {}).get("likeCount", 0)
            return stat or 0

        if up == 1:
            # 点赞
            if existing is not None:
                # 已是点赞态：幂等，不重复 +1
                return True, await _like_count()
            # 动态资源冗余写入 dynId（bizId=dynId）；非动态 dynId=NULL
            dyn_id = target_id if is_dynamic else None
            session.add(
                TMomentLike(
                    bizType=biz_type,
                    bizId=target_id,
                    dynId=dyn_id,
                    mid=mid,
                    likeType=1,
                )
            )
            await session.flush()
            if is_dynamic:
                await MomentStatService.incr_stat(session, target_id, "likeCount", 1)
            else:
                await InteractionStatService.incr(session, biz_type, target_id, "likeCount", 1)
            await session.commit()
            count = await _like_count()
            # 弱依赖：点赞事件通知给动态作者（P6-T6，独立会话，失败不影响点赞结果）
            if is_dynamic and dyn is not None:
                await MomentInteractionService._notify_like(mid, dyn)
            return True, count

        # 取消点赞（up == 2 或其余都视为取消）
        if existing is None:
            # 本来就没赞：幂等返回
            return False, await _like_count()
        await session.exec(  # type: ignore[call-overload]
            TMomentLike.__table__.delete().where(
                col(TMomentLike.pk) == existing
            )
        )
        if is_dynamic:
            await MomentStatService.decr_stat(
                session, target_id, "likeCount", floor_zero=True
            )
        else:
            await InteractionStatService.decr(session, biz_type, target_id, "likeCount")
        await session.commit()
        return False, await _like_count()

    # ==================== 举报（P4-T5）====================

    @staticmethod
    async def report(
        session: AsyncSession,
        reporter_mid: int,
        req: MomentReportReq,
    ) -> None:
        """举报动态（2.14.0 起改调统一举报服务，统一落库 `TReportRecord`，bizType=dynamic）。

        Raises:
            ValueError: 动态不存在 / 已软删 / 原因类型非法。
        """
        from app.models.schemas import ReportCreateReq
        from app.services.report import ReportService

        await ReportService.report(
            session,
            reporter_mid,
            ReportCreateReq(
                bizType=ReportBizTypeEnum.DYNAMIC.value,
                bizId=req.dynId,
                reasonType=req.reasonType,
                reasonDesc=req.reasonDesc,
                pics=None,
            ),
        )
        logger.info(f"用户 {reporter_mid} 举报动态 dynId={req.dynId}")

    # ==================== 点赞事件通知（P6-T6，弱依赖）====================

    @staticmethod
    async def _notify_like(actor_mid: int, dyn: TMoment) -> None:
        """弱依赖：通知被点赞动态的作者（LIKE + DYNAMIC）。

        独立会话投递：事件落库失败不影响点赞主事务结果。
        """
        from app.services.event import EventService

        try:
            async with new_session() as ns:
                await EventService.report(
                    ns,
                    EventReportReq(
                        mid=dyn.mid,
                        event_type=EventTypeEnum.LIKE,
                        source_type=SourceTypeEnum.DYNAMIC,
                        source_id=str(dyn.dynId),
                        actor_mid=actor_mid,
                        content=dyn.contentText,
                        biz_id=str(dyn.dynId),
                    ),
                )
        except Exception as e:  # noqa: BLE001
            logger.warning(f"点赞通知投递失败（弱依赖，已忽略）: {e}")

__all__ = ["MomentInteractionService"]
