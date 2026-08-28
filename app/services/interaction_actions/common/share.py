"""非动态资源通用分享上报互动（2.47.0 对象化）。

分享为行为上报（不幂等，可多次分享）；资源存在性经注册式校验器校验，
`shareCount` 原子 +1 走 `TInteractionStat`。
"""

from sqlmodel import col, select

from app.models.db import TInteractionStat
from app.models.schemas.interaction import InteractionResource
from app.services.moment.interaction import (
    BeMessageInteractionStatService as InteractionStatService,
    InteractionResourceValidator,
)
from app.services.interaction_actions.base import BaseInteractionAction


class ResourceShareAction(BaseInteractionAction):
    """非动态资源通用分享上报（行为上报，不幂等）。

    子类需声明 `_biz_type`（如 `lottery` / `rpa_action` 等）。
    """

    error_messages = {
        "not_found": "资源不存在或暂不可互动",
    }

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
        """分享计数原子 +1 并返回最新 shareCount。"""
        await InteractionStatService.incr(
            self.session, self.biz_type, self.biz_id, "shareCount", 1
        )
        await self.session.commit()
        stat = (
            await self.session.exec(
                select(TInteractionStat.shareCount).where(
                    col(TInteractionStat.bizType) == self.biz_type,
                    col(TInteractionStat.bizId) == self.biz_id,
                )
            )
        ).first()
        return stat or 0


__all__ = ["ResourceShareAction"]
