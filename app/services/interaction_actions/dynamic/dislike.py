"""动态（DYNAMIC）点踩互动操作（2.47.0 对象化；归入 `dynamic/` 子包）。

`dislikeCount` 供 EdgeRank `dislike_ratio` 降权使用。
"""

from sqlmodel import col, select

from app.models.db import TMoment, TMomentDislike, TInteractionStat
from app.models.enums import InteractionBizTypeEnum, MomentAuditStatusEnum
from app.models.schemas.interaction import InteractionResource
from app.services.interaction_actions.base import (
    BaseInteractionAction,
    InteractionActionError,
)
from app.services.moment.moment_stat import MomentStatService


class DislikeAction(BaseInteractionAction):
    """动态点踩 / 取消点踩（幂等，同一事务明细 + 计数双写）。"""

    _biz_type = InteractionBizTypeEnum.DYNAMIC
    error_messages = {
        "not_found": "动态不存在或暂不可互动",
        "invalid": "up 参数不合法（1=点踩, 2=取消点踩）",
    }

    def __init__(
        self,
        session,
        actor_mid: int,
        biz_id: int,
        up: int = 1,
        **ids,
    ):
        super().__init__(session, actor_mid, biz_id, **ids)
        self.up = int(up)

    # ==================== 基类接口 ====================

    async def get_resource(self) -> InteractionResource:
        """取 TMoment 并折叠为统一资源表示（仅 normal 未软删可互动）。"""
        dyn = (
            await self.session.exec(
                select(TMoment).where(col(TMoment.dynId) == self.biz_id)
            )
        ).one_or_none()
        if dyn is None or dyn.deletedAt is not None:
            return InteractionResource(
                bizType=InteractionBizTypeEnum.DYNAMIC,
                bizId=self.biz_id,
                exists=False,
            )
        return InteractionResource(
            bizType=InteractionBizTypeEnum.DYNAMIC,
            bizId=self.biz_id,
            authorMid=int(dyn.mid),
            exists=True,
            interactable=dyn.auditStatus == MomentAuditStatusEnum.NORMAL,
            content=dyn.contentText,
        )

    async def do_execute(self, resource):
        """点踩 / 取消点踩：明细幂等 + dislikeCount 原子 ±1，同一事务提交。"""
        if self.up not in (1, 2):
            raise InteractionActionError(self.error_messages["invalid"])
        target_id = self.biz_id

        existing = (
            await self.session.exec(
                select(TMomentDislike.pk).where(
                    col(TMomentDislike.bizType) == InteractionBizTypeEnum.DYNAMIC,
                    col(TMomentDislike.bizId) == target_id,
                    col(TMomentDislike.mid) == self.actor_mid,
                )
            )
        ).first()

        async def _count() -> int:
            stat = (
                await self.session.exec(
                    select(TInteractionStat.dislikeCount).where(
                        col(TInteractionStat.bizType) == InteractionBizTypeEnum.DYNAMIC,
                        col(TInteractionStat.bizId) == target_id,
                    )
                )
            ).first()
            return stat or 0

        if self.up == 1:
            # 点踩
            if existing is not None:
                return True, await _count()
            self.session.add(
                TMomentDislike(
                    bizType=InteractionBizTypeEnum.DYNAMIC,
                    bizId=target_id,
                    dynId=target_id,
                    mid=self.actor_mid,
                )
            )
            await self.session.flush()
            await MomentStatService.incr_stat(self.session, target_id, "dislikeCount", 1)
            await self.session.commit()
            return True, await _count()

        # 取消点踩（up == 2）
        if existing is None:
            return False, await _count()
        await self.session.exec(  # type: ignore[call-overload]
            TMomentDislike.__table__.delete().where(col(TMomentDislike.pk) == existing)
        )
        await MomentStatService.decr_stat(
            self.session, target_id, "dislikeCount", floor_zero=True
        )
        await self.session.commit()
        return False, await _count()


__all__ = ["DislikeAction"]
