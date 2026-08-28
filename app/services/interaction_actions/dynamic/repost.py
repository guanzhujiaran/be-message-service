"""动态（DYNAMIC）转发互动操作（2.47.0 对象化；归入 `dynamic/` 子包）。

转发为发布类互动：源动态须 normal 未软删，`do_execute` 复用
`MomentPublishService.repost`（审核流水 / Feed 元数据 / repostCount 状态机等
复杂逻辑保持一致）；关系权限 `NOT_BLOCKED`（任一向黑名单禁止转发）。
"""

from sqlmodel import col, select

from app.models.db import TMoment
from app.models.enums import InteractionBizTypeEnum, MomentAuditStatusEnum
from app.models.schemas.interaction import InteractionResource
from app.models.schemas.moment import MomentRepostReq
from app.services.interaction_actions.base import (
    BaseInteractionAction,
    InteractionActionError,
    InteractionRelationScopeEnum,
)
from app.services.moment.moment_publish import MomentPublishService


class RepostAction(BaseInteractionAction):
    """动态转发（FORWARD）：源动态须 normal 未软删，审核通过后源 repostCount +1。"""

    _biz_type = InteractionBizTypeEnum.DYNAMIC
    relation_scope = [InteractionRelationScopeEnum.NOT_BLOCKED]
    error_messages = {
        "not_found": "动态不存在",
        "invalid": "只能转发审核通过的动态",
    }

    def __init__(
        self,
        session,
        actor_mid: int,
        biz_id: int,
        content=None,
        **ids,
    ):
        super().__init__(session, actor_mid, biz_id, **ids)
        self.content = content

    # ==================== 基类接口 ====================

    async def get_resource(self) -> InteractionResource:
        """取源动态并折叠为统一资源表示（仅 normal 未软删可转发）。"""
        src = (
            await self.session.exec(
                select(TMoment).where(col(TMoment.dynId) == self.biz_id)
            )
        ).one_or_none()
        if src is None or src.deletedAt is not None:
            return InteractionResource(
                bizType=InteractionBizTypeEnum.DYNAMIC,
                bizId=self.biz_id,
                exists=False,
            )
        return InteractionResource(
            bizType=InteractionBizTypeEnum.DYNAMIC,
            bizId=self.biz_id,
            authorMid=int(src.mid),
            exists=True,
            interactable=src.auditStatus == MomentAuditStatusEnum.NORMAL,
            content=src.contentText,
        )

    async def do_execute(self, resource):
        """转发：复用 MomentPublishService.repost 创建 FORWARD 动态。"""
        return await MomentPublishService.repost(
            self.session,
            self.actor_mid,
            MomentRepostReq(srcDynId=self.biz_id, content=self.content),
            client_ip=getattr(self, "client_ip", None),
            user_agent=getattr(self, "user_agent", None),
        )


__all__ = ["RepostAction"]
