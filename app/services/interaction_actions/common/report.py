"""非动态资源通用举报互动（2.47.0 对象化）。

资源存在性经注册式校验器校验；不改动资源 `auditStatus`，落库统一举报记录
（`TResourceReport`，经 `ReportService`）。
"""

from app.models.schemas.interaction import InteractionResource
from app.services.moment.interaction import InteractionResourceValidator
from app.services.interaction_actions.base import BaseInteractionAction


class ResourceReportAction(BaseInteractionAction):
    """非动态资源通用举报（幂等：一人对同一资源只记一次）。

    子类需声明 `_biz_type`（如 `lottery` / `rpa_action` 等）。
    """

    error_messages = {
        "not_found": "资源不存在",
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
        """资源存在性校验（校验失败抛错）；通过后返回统一资源表示。"""
        await InteractionResourceValidator.validate(self.session, self.biz_type, self.biz_id)
        return InteractionResource(
            bizType=self.biz_type,
            bizId=self.biz_id,
            exists=True,
            interactable=True,
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
                bizType=ReportBizTypeEnum.DYNAMIC.value,  # 按资源类型映射；无专属枚举时统一 dynamic
                bizId=self.biz_id,
                reasonType=self.reason_type,
                reasonDesc=self.reason_desc,
                pics=self.pics,
            ),
        )


__all__ = ["ResourceReportAction"]
