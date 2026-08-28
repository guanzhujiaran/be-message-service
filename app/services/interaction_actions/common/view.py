"""非动态资源通用浏览上报互动（2.47.0 对象化）。

浏览为**弱依赖计数上报**（读路径副作用）：不校验资源存在（不存在仅无意义，不阻断），
每用户每资源一行明细（`TInteractionViewLog`），跨自然日再次访问才给 `viewCount` +1。
"""

from app.models.schemas.interaction import InteractionResource
from app.services.moment.interaction import (
    BeMessageInteractionStatService as InteractionStatService,
)
from app.services.interaction_actions.base import BaseInteractionAction


class ResourceViewAction(BaseInteractionAction):
    """非动态资源浏览上报（弱依赖计数，不校验资源存在）。

    子类需声明 `_biz_type`（如 `lottery` / `rpa_action` 等）。
    """

    def __init__(
        self,
        session,
        actor_mid: int,
        biz_id: int,
        **ids,
    ):
        super().__init__(session, actor_mid, biz_id, **ids)

    # ==================== 基类接口 ====================

    async def get_resource(self) -> InteractionResource:
        """浏览不校验资源（弱依赖），返回统一资源占位。"""
        return InteractionResource(
            bizType=self.biz_type,
            bizId=self.biz_id,
            exists=True,
            interactable=True,
        )

    async def check_resource_exists(self, resource: InteractionResource) -> None:
        """浏览为弱依赖计数上报：资源不存在不阻断（仅无意义）。"""

    async def do_execute(self, resource: InteractionResource):
        """浏览去重上报，返回 True=新计一次浏览量 / False=同日重复浏览。"""
        return await InteractionStatService.report_view(
            self.session, self.biz_type, self.biz_id, self.actor_mid
        )


__all__ = ["ResourceViewAction"]
