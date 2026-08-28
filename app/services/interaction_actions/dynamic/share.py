"""动态（DYNAMIC）分享上报互动操作（2.47.0 对象化；归入 `dynamic/` 子包）。

分享为行为上报（非幂等，可多次分享），normal 动态 ``shareCount`` 原子 +1，
计数供 EdgeRank share 权重使用。
"""

from sqlmodel import col, select

from app.models.db import TMoment, TInteractionStat
from app.models.enums import InteractionBizTypeEnum, MomentAuditStatusEnum
from app.models.schemas.interaction import InteractionResource
from app.services.interaction_actions.base import (
    BaseInteractionAction,
    InteractionActionError,
)
from app.services.moment.moment_stat import MomentStatService


class ShareAction(BaseInteractionAction):
    """动态分享上报：normal 动态 ``shareCount`` 原子 +1（行为上报，不幂等）。"""

    _biz_type = InteractionBizTypeEnum.DYNAMIC
    error_messages = {
        "not_found": "动态不存在或暂不可互动",
    }

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
        """分享计数原子 +1 并返回最新 shareCount。"""
        await MomentStatService.incr_stat(self.session, self.biz_id, "shareCount", 1)
        await self.session.commit()
        stat = (
            await self.session.exec(
                select(TInteractionStat.shareCount).where(
                    col(TInteractionStat.bizType) == InteractionBizTypeEnum.DYNAMIC,
                    col(TInteractionStat.bizId) == self.biz_id,
                )
            )
        ).first()
        return stat or 0


__all__ = ["ShareAction"]
