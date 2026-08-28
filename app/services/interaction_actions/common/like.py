"""非动态资源通用点赞互动（2.47.0 对象化）。

资源存在性经注册式校验器 `InteractionResourceValidator` 校验（已注册类型如
`lottery` 走 RPC；未注册类型默认放行）；明细写 `TMomentLike`（bizType+mid+likeType），
计数原子 ±1 走 `TInteractionStat`。
"""

from sqlalchemy.exc import IntegrityError
from sqlmodel import col, select

from app.models.db import TMomentLike, TInteractionStat
from app.models.schemas.interaction import InteractionResource
from app.services.moment.interaction import (
    BeMessageInteractionStatService as InteractionStatService,
    InteractionResourceValidator,
)
from app.services.interaction_actions.base import (
    BaseInteractionAction,
    InteractionActionError,
    InteractionRelationScopeEnum,
)


class ResourceLikeAction(BaseInteractionAction):
    """非动态资源通用点赞 / 取消点赞（幂等，明细 + 计数同事务）。

    子类需声明 `_biz_type`（如 `lottery` / `rpa_action` 等）。
    """

    relation_scope = [InteractionRelationScopeEnum.NOT_BLOCKED]
    error_messages = {
        "not_found": "资源不存在或暂不可互动",
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
        """资源存在性校验（校验失败抛错）；通过后返回统一资源表示（作者未知 → None）。"""
        await InteractionResourceValidator.validate(self.session, self.biz_type, self.biz_id)
        return InteractionResource(
            bizType=self.biz_type,
            bizId=self.biz_id,
            exists=True,
            interactable=True,
        )

    async def do_execute(self, resource):
        """点赞 / 取消点赞：明细幂等 + 计数原子 ±1，同一事务提交。"""
        if self.up not in (1, 2):
            raise InteractionActionError(self.error_messages["invalid"])
        target_id = self.biz_id

        existing = (
            await self.session.exec(
                select(TMomentLike.pk).where(
                    col(TMomentLike.bizType) == self.biz_type,
                    col(TMomentLike.bizId) == target_id,
                    col(TMomentLike.mid) == self.actor_mid,
                )
            )
        ).first()

        async def _count() -> int:
            counts = await InteractionStatService.batch_get_counts(
                self.session, self.biz_type, [target_id]
            )
            return counts.get(target_id, {}).get("likeCount", 0)

        if self.up == 1:
            # 点赞
            if existing is not None:
                # 已是点赞态：幂等，不重复 +1
                return True, await _count()
            self.session.add(
                TMomentLike(
                    bizType=self.biz_type,
                    bizId=target_id,
                    dynId=None,
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
            await InteractionStatService.incr(
                self.session, self.biz_type, target_id, "likeCount", 1
            )
            await self.session.commit()
            return True, await _count()

        # 取消点赞（up == 2）
        if existing is None:
            # 本来就没赞：幂等返回
            return False, await _count()
        await self.session.exec(  # type: ignore[call-overload]
            TMomentLike.__table__.delete().where(col(TMomentLike.pk) == existing)
        )
        await InteractionStatService.decr(
            self.session, self.biz_type, target_id, "likeCount"
        )
        await self.session.commit()
        return False, await _count()


__all__ = ["ResourceLikeAction"]
