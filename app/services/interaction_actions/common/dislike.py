"""非动态资源通用点踩互动（2.47.0 对象化）。

资源存在性经注册式校验器校验；明细写 `TMomentDislike`（bizType+mid），
计数原子 ±1 走 `TInteractionStat.dislikeCount`。
"""

from sqlalchemy.exc import IntegrityError
from sqlmodel import col, delete, select

from app.models.db import TMomentDislike, TInteractionStat
from app.models.schemas.interaction import InteractionResource
from app.services.moment.interaction import (
    BeMessageInteractionStatService as InteractionStatService,
    InteractionResourceValidator,
)
from app.services.interaction_actions.base import (
    BaseInteractionAction,
    InteractionActionError,
)


class ResourceDislikeAction(BaseInteractionAction):
    """非动态资源通用点踩 / 取消点踩（幂等，明细 + 计数同事务）。

    子类需声明 `_biz_type`（如 `lottery` / `rpa_action` 等）。
    """

    error_messages = {
        "not_found": "资源不存在或暂不可互动",
        "invalid": "up 参数不合法（1=点踩, 2=取消点踩）",
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
        """资源存在性校验（校验失败抛错）；通过后返回统一资源表示。"""
        await InteractionResourceValidator.validate(self.session, self.biz_type, self.biz_id)
        return InteractionResource(
            bizType=self.biz_type,
            bizId=self.biz_id,
            exists=True,
            interactable=True,
        )

    async def do_execute(self, resource: InteractionResource):
        """点踩 / 取消点踩：明细幂等 + dislikeCount 原子 ±1，同一事务提交。"""
        if self.up not in (1, 2):
            raise InteractionActionError(self.error_messages["invalid"])
        target_id = self.biz_id

        existing = (
            await self.session.exec(
                select(TMomentDislike.pk).where(
                    col(TMomentDislike.bizType) == self.biz_type,
                    col(TMomentDislike.bizId) == target_id,
                    col(TMomentDislike.mid) == self.actor_mid,
                )
            )
        ).first()

        async def _count() -> int:
            counts = await InteractionStatService.batch_get_counts(
                self.session, self.biz_type, [target_id]
            )
            return counts.get(target_id, {}).get("dislikeCount", 0)

        if self.up == 1:
            if existing is not None:
                return True, await _count()
            self.session.add(
                TMomentDislike(
                    bizType=self.biz_type,
                    bizId=target_id,
                    dynId=None,
                    mid=self.actor_mid,
                )
            )
            try:
                await self.session.flush()
            except IntegrityError:
                await self.session.rollback()
                return True, await _count()
            await InteractionStatService.incr(
                self.session, self.biz_type, target_id, "dislikeCount", 1
            )
            await self.session.commit()
            return True, await _count()

        if existing is None:
            return False, await _count()
        await self.session.exec(
            delete(TMomentDislike).where(
                col(TMomentDislike.bizType) == self.biz_type,
                col(TMomentDislike.bizId) == target_id,
                col(TMomentDislike.mid) == self.actor_mid,
            )
        )
        await InteractionStatService.decr(
            self.session, self.biz_type, target_id, "dislikeCount"
        )
        await self.session.commit()
        return False, await _count()


__all__ = ["ResourceDislikeAction"]
