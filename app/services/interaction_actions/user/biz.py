"""用户（USER）资源类（2.48.0）。

用户作为可举报资源纳入统一模型（bizId = mid）：

- **落库表**：`TUserReport`；
- **存在性 / 作者回查**：经 pptr Postgres 的 `PptrUserInfo` 校验（未软删），
  被举报人即该用户自身，故 `resolve_accused()` 返回 `biz_id`；
- **下架**：**预留**——用户维度处置（禁言 / 封禁）待后续，故 `hide()` 为空实现
  （不抛错，保持举报审核流程可走通）。
"""

from sqlmodel import col, select

from app.core.database import new_pptr_session
from app.models.db.report_tbl import TUserReport
from bili_common.models import InteractionBizTypeEnum
from app.models.enums import ResourceAuditStatusEnum
from app.models.pptr_db import PptrUserInfo
from app.models.schemas.interaction import InteractionResource
from app.services.interaction_actions.base import InteractionAclScopeEnum
from app.services.interaction_actions.base_biz import BaseBiz, biz_action

__all__ = ["UserBiz"]


class UserBiz(BaseBiz):
    """用户资源。"""

    _biz_type = InteractionBizTypeEnum.USER
    model = TUserReport
    error_messages = {
        "not_found": "用户不存在",
        "invalid": "参数不合法",
    }

    async def _exists(self) -> bool:
        """pptr Postgres 校验用户存在（未软删）。"""
        if self.biz_id <= 0:
            return False
        async with new_pptr_session() as ps:
            row = (
                await ps.exec(
                    select(PptrUserInfo.uid).where(
                        col(PptrUserInfo.uid) == int(self.biz_id),
                        col(PptrUserInfo.deletedAt).is_(None),
                    )
                )
            ).first()
        return row is not None

    # ==================== 资源获取（钩子实现）====================

    async def check_exists(self) -> bool:
        return await self._exists()

    async def _load_meta(self) -> tuple[str | None, str | None]:
        if not await self._exists():
            return None, None
        try:
            async with new_pptr_session() as ps:
                row = (
                    await ps.exec(
                        select(PptrUserInfo).where(
                            col(PptrUserInfo.uid) == int(self.biz_id)
                        )
                    )
                ).first()
        except Exception:  # noqa: BLE001
            return None, None
        if row is None:
            return None, None
        return (getattr(row, "uname", None), getattr(row, "avatar", None))

    async def _load_author_mid(self) -> int | None:
        return int(self.biz_id) if self.biz_id and self.biz_id > 0 else None

    # ==================== 举报 ====================

    @biz_action()
    async def report(self, reason_type, reason_desc: str | None = None, pics=None):
        """举报用户（幂等）。返回 (created, triggered)。"""
        if self.actor_mid is None:
            raise ValueError("举报需登录用户")
        from app.models.schemas import ReportCreateReq
        from app.services.admin.report import ReportService

        return await ReportService.report(
            self.session,
            self.actor_mid,
            ReportCreateReq(
                bizType=InteractionBizTypeEnum.USER.value,
                bizId=self.biz_id,
                reasonType=reason_type,
                reasonDesc=reason_desc,
                pics=pics,
            ),
        )

    async def resolve_accused(self) -> int:
        """校验被举报用户存在，返回其 mid。"""
        if not await self._exists():
            raise ValueError("用户不存在")
        return int(self.biz_id)

    # ==================== 审核（头像审核，bizId = mid）====================
    # 头像审核记录按 pk 定位，这里以 mid 反查该用户「待审核」的最新一条申请，
    # 再委托 AvatarAuditService（通过 / 驳回均已内置系统通知）。

    async def _pending_avatar_pk(self) -> int | None:
        from app.models.db.avatar_audit_tbl import TUserAvatarAudit

        row = (
            await self.session.exec(
                select(TUserAvatarAudit)
                .where(
                    col(TUserAvatarAudit.mid) == int(self.biz_id),
                    col(TUserAvatarAudit.auditStatus)
                    == ResourceAuditStatusEnum.AUDITING,
                )
                .order_by(col(TUserAvatarAudit.pk).desc())
                .limit(1)
            )
        ).one_or_none()
        return int(row.pk) if row is not None else None

    @biz_action(acl=[InteractionAclScopeEnum.AUDITOR_ONLY], require_resource=False)
    async def audit_approve(self, remark: str | None = None, **kwargs):
        """头像审核通过：写入公开头像 + 通知用户（由 AvatarAuditService 承载）。"""
        from app.services.user.avatar_audit import AvatarAuditService

        pk = await self._pending_avatar_pk()
        if pk is None:
            raise ValueError("该用户没有待审核的头像申请")
        return await AvatarAuditService.approve(
            self.session, pk, operator_mid=int(self.actor_mid or 0), remark=remark
        )

    @biz_action(acl=[InteractionAclScopeEnum.AUDITOR_ONLY], require_resource=False)
    async def audit_reject(
        self, reject_reason: str | None = None, remark: str | None = None, **kwargs
    ):
        """头像审核驳回：保留原头像 + 通知用户并附驳回原因。"""
        from app.services.user.avatar_audit import AvatarAuditService

        pk = await self._pending_avatar_pk()
        if pk is None:
            raise ValueError("该用户没有待审核的头像申请")
        return await AvatarAuditService.reject(
            self.session,
            pk,
            operator_mid=int(self.actor_mid or 0),
            reason=reject_reason or remark or "",
            remark=remark,
        )

    async def hide(self, **kwargs) -> None:
        """用户维度处置（禁言 / 封禁）**预留**，当前不执行任何动作。

        `operator_mid` 等参数经 `**kwargs` 透传以兼容 `BaseBiz.hide` 签名。
        """

    # ==================== 关注 / 拉黑（仅 USER 资源）====================
    # 这些关系操作仅 USER 资源使用，故置于 UserBiz 而非 BaseBiz 接口；
    # biz_id = 被操作的目标用户 mid，actor_mid = 当前登录用户（操作者）。

    @biz_action()
    async def follow(self):
        """关注 biz_id 对应的用户（即 target = self.biz_id）。

        actor 为当前登录用户（self.actor_mid）。幂等：已关注保持 following；
        此前拉黑则翻转为 following；对方已拉黑自己则拒绝。
        """
        if self.actor_mid is None:
            raise ValueError("关注需登录用户")
        from app.services.user.follow import FollowService

        return await FollowService.follow(self.session, self.actor_mid, self.biz_id)

    @biz_action()
    async def unfollow(self):
        """取关 biz_id 对应的用户。幂等：未关注也返回成功。"""
        if self.actor_mid is None:
            raise ValueError("取关需登录用户")
        from app.services.user.follow import FollowService

        return await FollowService.unfollow(self.session, self.actor_mid, self.biz_id)

    @biz_action()
    async def block(self):
        """拉黑 biz_id 对应的用户：状态翻转为 blocked，并删除对方对自己的 following。"""
        if self.actor_mid is None:
            raise ValueError("拉黑需登录用户")
        from app.services.user.follow import FollowService

        return await FollowService.block(self.session, self.actor_mid, self.biz_id)

    @biz_action()
    async def unblock(self):
        """解除对 biz_id 用户的拉黑。幂等：未拉黑也返回成功。"""
        if self.actor_mid is None:
            raise ValueError("解除拉黑需登录用户")
        from app.services.user.follow import FollowService

        return await FollowService.unblock(self.session, self.actor_mid, self.biz_id)
