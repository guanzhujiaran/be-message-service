"""动态（DYNAMIC）审核操作对象化（2.47.0；DAC：仅审核员可操作）。

审核通过 / 驳回从原 `MomentAuditService` 静态方法迁移为操作类：

- `AuditApproveAction`：auditStatus→normal + pubTime=now()；写 AuditLog；**不发通知**；
  FORWARD 时源动态 repostCount +1（状态机触发点①）；
- `AuditRejectAction`：auditStatus→rejected + 写 auditRejectReason；写 AuditLog；
  **发驳回事件通知给作者**（AUDIT_REJECT）；FORWARD ∧ before=normal 时源动态 repostCount -1（②）。

两者 `acl_scope = [AUDITOR_ONLY]`：围绕资源展开的 DAC 权限控制，默认 `_is_auditor()=True`
（审核接口在鉴权层已用 `RootUser` 保证管理员），子类可覆盖为真实审核员校验。
"""

from datetime import datetime

from loguru import logger
from sqlmodel import col, select

from app.models.db import TMoment
from app.models.enums import (
    InteractionBizTypeEnum,
    MomentAuditLogActionEnum,
    MomentAuditStatusEnum,
    MomentTypeEnum,
)
from app.models.schemas.interaction import InteractionResource
from app.services.interaction_actions.base import (
    BaseInteractionAction,
    InteractionAclScopeEnum,
)
from app.services.moment.moment_audit import (
    _build_audit_log,
    _get_any,
    _safe_author_brief,
    _sync_resource_feed,
    _to_audit_item,
)
from app.services.moment.moment_stat import MomentStatService


class AuditApproveAction(BaseInteractionAction):
    """动态审核通过（DAC：仅审核员）。"""

    _biz_type = InteractionBizTypeEnum.DYNAMIC
    acl_scope = [InteractionAclScopeEnum.AUDITOR_ONLY]
    error_messages = {
        "not_found": "动态不存在",
    }

    def __init__(
        self,
        session,
        actor_mid: int,
        biz_id: int,
        remark: str | None = None,
        **ids,
    ):
        super().__init__(session, actor_mid, biz_id, **ids)
        self.remark = remark

    # ==================== 基类接口 ====================

    async def get_resource(self) -> InteractionResource:
        """取动态（含全部状态，审核后台可对 auditing/normal/rejected 操作）。"""
        dyn = await _get_any(self.session, self.biz_id)
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
            ownerMid=int(dyn.mid),
            exists=True,
            interactable=True,
            content=dyn.contentText,
        )

    async def do_execute(self, resource: InteractionResource):
        """审核通过：auditStatus→normal + pubTime=now()；写 AuditLog；FORWARD 源 +1。"""
        dyn = await _get_any(self.session, self.biz_id)
        if dyn is None:
            raise ValueError(self.error_messages["not_found"])
        from_status = dyn.auditStatus
        now = datetime.now()
        dyn.auditStatus = MomentAuditStatusEnum.NORMAL
        dyn.pubTime = now
        dyn.updated_at = now
        # 2.36.0：同步通用 Feed 元数据（normal + pubTime，此后入 Feed）
        await _sync_resource_feed(
            self.session, self.biz_id, audit_status="normal", pub_time=now
        )
        # 状态机触发点①：FORWARD 且源动态存在 → 源动态 repostCount +1
        if dyn.dynType is MomentTypeEnum.FORWARD and dyn.repostSrcDynId:
            await MomentStatService.incr_repost_count(self.session, dyn.repostSrcDynId, 1)
        self.session.add(
            _build_audit_log(
                moment_id=self.biz_id,
                operator_mid=self.actor_mid,
                to_status=MomentAuditStatusEnum.NORMAL,
                action=MomentAuditLogActionEnum.APPROVE,
                from_status=from_status,
                remark=self.remark,
            )
        )
        await self.session.commit()
        await self.session.refresh(dyn)
        logger.info(f"管理员 {self.actor_mid} 审核通过动态 dynId={self.biz_id}")
        author = await _safe_author_brief(dyn.mid)
        return _to_audit_item(dyn, author)


class AuditRejectAction(BaseInteractionAction):
    """动态审核驳回（DAC：仅审核员）。"""

    _biz_type = InteractionBizTypeEnum.DYNAMIC
    acl_scope = [InteractionAclScopeEnum.AUDITOR_ONLY]
    error_messages = {
        "not_found": "动态不存在",
    }

    def __init__(
        self,
        session,
        actor_mid: int,
        biz_id: int,
        reject_reason: str,
        remark: str | None = None,
        **ids,
    ):
        super().__init__(session, actor_mid, biz_id, **ids)
        self.reject_reason = reject_reason
        self.remark = remark

    # ==================== 基类接口 ====================

    async def get_resource(self) -> InteractionResource:
        dyn = await _get_any(self.session, self.biz_id)
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
            ownerMid=int(dyn.mid),
            exists=True,
            interactable=True,
            content=dyn.contentText,
        )

    async def do_execute(self, resource: InteractionResource):
        """审核驳回：auditStatus→rejected + 写 auditRejectReason；写 AuditLog；
        发驳回通知给作者；FORWARD ∧ before=normal 时源 repostCount -1。"""
        from app.services.moment.moment_audit import _notify_reject

        dyn = await _get_any(self.session, self.biz_id)
        if dyn is None:
            raise ValueError(self.error_messages["not_found"])
        from_status = dyn.auditStatus
        before_normal = from_status == MomentAuditStatusEnum.NORMAL
        now = datetime.now()
        dyn.auditStatus = MomentAuditStatusEnum.REJECTED
        dyn.auditRejectReason = self.reject_reason
        dyn.updated_at = now
        # 2.36.0：同步通用 Feed 元数据（rejected，不入 Feed）
        await _sync_resource_feed(self.session, self.biz_id, audit_status="rejected")
        # 状态机触发点②：FORWARD ∧ before=normal → 源动态 repostCount -1
        if (
            dyn.dynType is MomentTypeEnum.FORWARD
            and dyn.repostSrcDynId
            and before_normal
        ):
            await MomentStatService.incr_repost_count(self.session, dyn.repostSrcDynId, -1)
        self.session.add(
            _build_audit_log(
                moment_id=self.biz_id,
                operator_mid=self.actor_mid,
                to_status=MomentAuditStatusEnum.REJECTED,
                action=MomentAuditLogActionEnum.REJECT,
                from_status=from_status,
                reject_reason=self.reject_reason,
                remark=self.remark,
            )
        )
        await self.session.commit()
        await self.session.refresh(dyn)
        logger.info(f"管理员 {self.actor_mid} 审核驳回动态 dynId={self.biz_id}：{self.reject_reason}")
        # 弱依赖：通知作者（独立会话，失败不影响审核结果）
        await _notify_reject(self.actor_mid, dyn, self.reject_reason)
        author = await _safe_author_brief(dyn.mid)
        return _to_audit_item(dyn, author)


__all__ = ["AuditApproveAction", "AuditRejectAction"]
