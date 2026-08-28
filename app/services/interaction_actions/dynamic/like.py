"""动态（DYNAMIC）点赞互动操作（2.47.0 对象化；归入 `dynamic/` 子包）。

动态资源专属：资源对象取 `TMoment`（仅 normal 未软删）；点赞成功发 LIKE 事件通知作者。
"""

from loguru import logger
from sqlalchemy.exc import IntegrityError
from sqlmodel import col, select

from app.models.db import TMoment, TMomentLike, TInteractionStat
from app.models.enums import InteractionBizTypeEnum, MomentAuditStatusEnum
from app.models.schemas.interaction import InteractionResource
from app.services.interaction_actions.base import (
    BaseInteractionAction,
    InteractionActionError,
    InteractionRelationScopeEnum,
)
from app.services.moment.moment_stat import MomentStatService


class LikeAction(BaseInteractionAction):
    """动态点赞 / 取消点赞（幂等，同一事务内明细 + 计数双写）。

    - 资源类型：`_biz_type = DYNAMIC`（不可变，类声明绑定）；
    - 关系权限：`[NOT_BLOCKED]`（任一向黑名单禁止点赞）；
    - 操作后 hook：点赞成功时通知被赞动态作者（LIKE 事件，弱依赖独立会话）。
    """

    _biz_type = InteractionBizTypeEnum.DYNAMIC
    relation_scope = [InteractionRelationScopeEnum.NOT_BLOCKED]
    error_messages = {
        "not_found": "动态不存在或暂不可互动",
        "invalid": "up 参数不合法（1=点赞, 2=取消点赞）",
    }

    def __init__(
        self,
        session,
        actor_mid: int,
        biz_id: int,
        up: int = 1,
        **ids,
    ):
        super().__init__(session, actor_mid, biz_id, **ids)
        self.up = int(up)

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
        interactable = dyn.auditStatus == MomentAuditStatusEnum.NORMAL
        return InteractionResource(
            bizType=InteractionBizTypeEnum.DYNAMIC,
            bizId=self.biz_id,
            authorMid=int(dyn.mid),
            exists=True,
            interactable=interactable,
            content=dyn.contentText,
        )

    async def do_execute(self, resource):
        """点赞 / 取消点赞：明细幂等 + 计数原子 ±1，同一事务提交。"""
        if self.up not in (1, 2):
            raise InteractionActionError(self.error_messages["invalid"])
        target_id = self.biz_id

        existing = (
            await self.session.exec(
                select(TMomentLike.pk).where(
                    col(TMomentLike.bizType) == InteractionBizTypeEnum.DYNAMIC,
                    col(TMomentLike.bizId) == target_id,
                    col(TMomentLike.mid) == self.actor_mid,
                )
            )
        ).first()

        async def _count() -> int:
            stat = (
                await self.session.exec(
                    select(TInteractionStat.likeCount).where(
                        col(TInteractionStat.bizType) == InteractionBizTypeEnum.DYNAMIC,
                        col(TInteractionStat.bizId) == target_id,
                    )
                )
            ).first()
            return stat or 0

        if self.up == 1:
            # 点赞
            if existing is not None:
                # 已是点赞态：幂等，不重复 +1
                return True, await _count()
            self.session.add(
                TMomentLike(
                    bizType=InteractionBizTypeEnum.DYNAMIC,
                    bizId=target_id,
                    dynId=target_id,
                    mid=self.actor_mid,
                    likeType=1,
                )
            )
            try:
                await self.session.flush()
            except IntegrityError:
                # 并发重复点赞（同用户对同资源）：唯一约束 (bizType,bizId,mid) 冲突，幂等返回
                await self.session.rollback()
                return True, await _count()
            await MomentStatService.incr_stat(self.session, target_id, "likeCount", 1)
            await self.session.commit()
            return True, await _count()

        # 取消点赞（up == 2）
        if existing is None:
            # 本来就没赞：幂等返回
            return False, await _count()
        await self.session.exec(  # type: ignore[call-overload]
            TMomentLike.__table__.delete().where(col(TMomentLike.pk) == existing)
        )
        await MomentStatService.decr_stat(
            self.session, target_id, "likeCount", floor_zero=True
        )
        await self.session.commit()
        return False, await _count()

    async def after_execute(self, resource: InteractionResource, result):
        """点赞成功时通知被赞动态作者（LIKE 事件，弱依赖，失败不影响主事务）。"""
        if resource is None:
            return
        is_like, _ = result
        if not is_like:
            return
        from app.core.database import new_session
        from app.models.enums import EventTypeEnum, SourceTypeEnum
        from app.models.schemas import EventReportReq
        from app.services.message.event import EventService

        try:
            async with new_session() as ns:
                await EventService.report(
                    ns,
                    EventReportReq(
                        mid=resource.authorMid,
                        event_type=EventTypeEnum.LIKE,
                        source_type=SourceTypeEnum.DYNAMIC,
                        source_id=str(resource.bizId),
                        actor_mid=self.actor_mid,
                        content=resource.content,
                        biz_id=str(resource.bizId),
                    ),
                )
        except Exception as e:  # noqa: BLE001
            logger.warning(f"点赞通知投递失败（弱依赖，已忽略）: {e}")


__all__ = ["LikeAction"]
