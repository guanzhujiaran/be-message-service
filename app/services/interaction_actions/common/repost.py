"""非动态资源通用转发 / attach 互动（2.47.0 对象化）。

对非动态资源（lottery / rpa_*），「转发」语义 = **attach**：把资源挂到卡片 / 内容
上，`repostCount` 原子 +1 记录 attach 次数（复用 `TInteractionStat.repostCount`，
不再额外建明细表）；`attach_to` 记录 attach 目标（卡片 / 内容 id，可选）。
"""

from sqlmodel import col, select

from app.models.db import TInteractionStat
from app.models.schemas.interaction import InteractionResource
from app.services.moment.interaction import (
    BeMessageInteractionStatService as InteractionStatService,
    InteractionResourceValidator,
)
from app.services.interaction_actions.base import BaseInteractionAction


class ResourceRepostAction(BaseInteractionAction):
    """非动态资源通用转发 / attach（行为计数 `repostCount` +1，不幂等）。

    子类需声明 `_biz_type`（如 `lottery` / `rpa_action` 等）。
    """

    error_messages = {
        "not_found": "资源不存在或暂不可互动",
    }

    def __init__(
        self,
        session,
        actor_mid: int,
        biz_id: int,
        attach_to: int | None = None,
        **ids,
    ):
        super().__init__(session, actor_mid, biz_id, **ids)
        self.attach_to = attach_to  # attach 目标（卡片 / 内容 id，可选）

    # ==================== 基类接口 ====================

    async def get_resource(self) -> InteractionResource:
        """资源存在性校验（校验失败抛错）；通过后返回统一资源表示。"""
        await InteractionResourceValidator.validate(self.session, self.biz_type, self.biz_id)
        return InteractionResource(
            bizType=self.biz_type,
            bizId=self.biz_id,
            exists=True,
            interactable=True,
        )

    async def do_execute(self, resource: InteractionResource):
        """attach 行为计数原子 +1，返回最新 repostCount（attach 次数）。"""
        await InteractionStatService.incr(
            self.session, self.biz_type, self.biz_id, "repostCount", 1
        )
        await self.session.commit()
        stat = (
            await self.session.exec(
                select(TInteractionStat.repostCount).where(
                    col(TInteractionStat.bizType) == self.biz_type,
                    col(TInteractionStat.bizId) == self.biz_id,
                )
            )
        ).first()
        return stat or 0


__all__ = ["ResourceRepostAction"]
