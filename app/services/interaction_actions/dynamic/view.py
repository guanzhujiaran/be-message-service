"""动态（DYNAMIC）浏览上报互动（2.47.0 对象化）。

浏览为弱依赖计数上报：不校验动态存在（不存在仅无意义，不阻断），
`TInteractionViewLog` 每用户每动态一行，跨自然日再次访问才给 `viewCount` +1。
"""

from app.models.enums import InteractionBizTypeEnum
from app.models.schemas.interaction import InteractionResource
from app.services.interaction_actions.common.view import ResourceViewAction
from app.services.moment.moment_stat import MomentStatService


class ViewAction(ResourceViewAction):
    """动态浏览上报（弱依赖计数，不校验动态存在）。"""

    _biz_type = InteractionBizTypeEnum.DYNAMIC

    async def do_execute(self, resource: InteractionResource):
        """浏览去重上报（动态走 MomentStatService），返回 True=新计一次浏览量。"""
        return await MomentStatService.report_view(
            self.session, self.biz_id, self.actor_mid
        )


__all__ = ["ViewAction"]
