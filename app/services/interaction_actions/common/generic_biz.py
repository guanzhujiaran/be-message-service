"""通用资源基类（2.48.0）：非动态内容型资源（lottery / rpa_*）的共享实现。

通用资源行为高度一致——资源存在性经注册式校验器校验，计数走 `TInteractionStat`，
举报落 `TResourceReport` 且处置为「Feed 层退出 + RPC 通知归属服务」双层。
因此本类一次性实现全部通用操作，`lottery` / `rpa_*` 只需继承并声明 `_biz_type`；
个别差异（如 lottery 禁止下架）在子类覆盖对应方法即可。
"""

from loguru import logger
from sqlmodel import col, select

from app.models.db import TResourceFeed
from app.models.db.moment_tbl import TResourceReport
from app.models.enums import InteractionBizTypeEnum
from app.models.schemas.interaction import InteractionResource
from app.services.interaction_actions.base import InteractionRelationScopeEnum
from app.services.interaction_actions.base_biz import BaseBiz, biz_action
from app.services.interaction_actions.common import ops

__all__ = ["GenericResourceBiz"]


class GenericResourceBiz(BaseBiz):
    """通用内容型资源基类（子类需声明 `_biz_type`）。"""

    model = TResourceReport
    error_messages = {
        "not_found": "资源不存在或暂不可互动",
        "invalid": "参数不合法",
    }

    # ==================== 资源获取 ====================

    async def get_resource(self) -> InteractionResource:
        """资源存在性校验（注册式校验器；未注册类型默认放行）。"""
        await ops.validate_exists(self.session, self.biz_type, self.biz_id)
        return InteractionResource(
            bizType=self.biz_type,
            bizId=self.biz_id,
            exists=True,
            interactable=True,
        )

    # ==================== 互动操作 ====================

    @biz_action()
    async def like(self, up: int = 1):
        """点赞 / 取消点赞。返回 (是否点赞中, 最新 likeCount)。"""
        return await ops.do_like_generic(
            self.session, self.biz_type, self.biz_id, self.actor_mid, up
        )

    @biz_action()
    async def dislike(self, up: int = 1):
        """点踩 / 取消点踩。返回 (是否点踩中, 最新 dislikeCount)。"""
        return await ops.do_dislike(
            self.session, self.biz_type, self.biz_id, self.actor_mid, up
        )

    @biz_action(relation=[InteractionRelationScopeEnum.NOT_BLOCKED])
    async def favorite(self, action: str = "add", folder_id=None):
        """收藏 / 取消收藏。返回 (是否变更, 目标 folder_id)。"""
        return await ops.do_favorite(
            self.session, self.biz_type, self.biz_id, self.actor_mid, folder_id, action
        )

    @biz_action()
    async def share(self):
        """分享上报（不幂等）。返回最新 shareCount。"""
        return await ops.do_share(self.session, self.biz_type, self.biz_id)

    @biz_action()
    async def repost(self, attach_to: int | None = None):
        """转发 / attach（通用资源 = attach 行为计数）。返回最新 repostCount。"""
        return await ops.do_repost(self.session, self.biz_type, self.biz_id, attach_to)

    @biz_action(require_resource=False)
    async def view(self):
        """浏览上报（弱依赖，不校验资源存在）。返回 True=新计一次。"""
        return await ops.do_view(
            self.session, self.biz_type, self.biz_id, self.actor_mid
        )

    # ==================== 评论类操作（reply / at）====================

    @biz_action()
    async def reply(self, content: str, *, at_mids=None, pictures=None, emote_meta=None, up_mid=0):
        """在本资源下发表评论（一级，root=0）。返回 CommentAddResp。"""
        return await ops.do_comment(
            self.session, self.biz_type, self.biz_id, self.actor_mid,
            root=0, message=content, at_mids=at_mids,
            pictures=pictures, emote_meta=emote_meta, up_mid=up_mid,
        )

    @biz_action()
    async def at(self, mids, content: str, *, pictures=None, emote_meta=None, up_mid=0):
        """在本资源下 @ 提及用户（一级，root=0）。返回 CommentAddResp。"""
        return await ops.do_comment(
            self.session, self.biz_type, self.biz_id, self.actor_mid,
            root=0, message=content, at_mids=list(mids),
            pictures=pictures, emote_meta=emote_meta, up_mid=up_mid,
        )

    # ==================== 举报 ====================

    @biz_action()
    async def report(self, reason_type, reason_desc: str | None = None, pics=None):
        """举报本资源（幂等）。返回 (created, triggered)。"""
        from app.models.schemas import ReportCreateReq
        from app.services.admin.report import ReportService

        return await ReportService.report(
            self.session,
            self.actor_mid,
            ReportCreateReq(
                bizType=int(self.biz_type),
                bizId=self.biz_id,
                reasonType=reason_type,
                reasonDesc=reason_desc,
                pics=pics,
            ),
        )

    async def resolve_accused(self) -> int:
        """经归属服务 RPC 取资源作者 mid（弱依赖，失败返回 0 = 未知作者）。"""
        from app.services.infrastructure.rpa_rpc import rpa_rpc_client

        try:
            detail = await rpa_rpc_client.get_resource_detail(
                self.biz_type.to_text(), int(self.biz_id)
            )
            if detail and detail.detail and detail.detail.authorMid:
                try:
                    return int(detail.detail.authorMid)
                except (TypeError, ValueError):
                    return 0
        except Exception:  # noqa: BLE001
            logger.warning(
                f"举报资源作者回查失败: biz_type={self.biz_type!r} bizId={self.biz_id}"
            )
        return 0

    async def hide(self, *, operator_mid: int = 0, **kwargs) -> None:
        """双层处置：① 本地 Feed 退出（立即生效）② RPC 通知归属服务下架（弱依赖）。"""
        feed = (
            await self.session.exec(
                select(TResourceFeed).where(
                    col(TResourceFeed.bizType) == self.biz_type,
                    col(TResourceFeed.bizId) == self.biz_id,
                )
            )
        ).one_or_none()
        if feed is not None:
            feed.auditStatus = "hidden"
        await self.session.commit()

        from app.services.infrastructure.rpa_rpc import rpa_rpc_client

        try:
            await rpa_rpc_client.hide_resource(
                biz_type=self.biz_type.to_text(),
                biz_id=self.biz_id,
                operator_mid=operator_mid,
                reason="举报下架",
            )
        except Exception:  # noqa: BLE001
            logger.warning(
                f"举报处置 RPC 失败（资源层降级）: {self.biz_type!r}/{self.biz_id}"
            )
