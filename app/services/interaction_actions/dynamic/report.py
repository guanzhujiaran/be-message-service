"""动态（DYNAMIC）举报互动操作（2.47.0 对象化）。

举报不改动资源 `auditStatus`（2.40.0 处置模型：举报达阈值仅「加入审核队列」，
是否下架由管理员在举报审核中显式决定）；底层复用统一举报服务 `ReportService`。
"""

from sqlmodel import col, select

from app.models.db import TMoment
from app.models.enums import InteractionBizTypeEnum, MomentAuditStatusEnum
from app.models.schemas.interaction import InteractionResource
from app.services.interaction_actions.base import (
    BaseInteractionAction,
    InteractionActionError,
)


class ReportAction(BaseInteractionAction):
    """举报动态（幂等：一人对同一动态只记一次）。

    Returns:
        (created, triggered)：是否新增举报；是否达阈值标记（不触发任何自动处置）。
    """

    _biz_type = InteractionBizTypeEnum.DYNAMIC
    error_messages = {
        "not_found": "动态不存在",
        "invalid": "非法的举报原因",
    }

    def __init__(
        self,
        session,
        actor_mid: int,
        biz_id: int,
        reason_type,
        reason_desc: str | None = None,
        pics: list[str] | None = None,
        **ids,
    ):
        super().__init__(session, actor_mid, biz_id, **ids)
        self.reason_type = reason_type
        self.reason_desc = reason_desc
        self.pics = pics

    # ==================== 基类接口 ====================

    async def get_resource(self) -> InteractionResource:
        """取被举报动态并折叠为统一资源表示（软删视为不存在；举报不限制 auditStatus）。"""
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
            interactable=True,
            content=dyn.contentText,
        )

    async def do_execute(self, resource: InteractionResource):
        """复用统一举报服务落库 `TResourceReport`（不改 auditStatus）。"""
        from bili_common.models.report import ReportBizTypeEnum
        from app.models.schemas import ReportCreateReq
        from app.services.admin.report import ReportService

        return await ReportService.report(
            self.session,
            self.actor_mid,
            ReportCreateReq(
                bizType=ReportBizTypeEnum.DYNAMIC.value,
                bizId=self.biz_id,
                reasonType=self.reason_type,
                reasonDesc=self.reason_desc,
                pics=self.pics,
            ),
        )


__all__ = ["ReportAction"]
