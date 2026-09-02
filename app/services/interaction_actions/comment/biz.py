"""评论（COMMENT）资源类（2.48.0）。

评论作为可举报资源纳入统一模型：

- **落库表**：`CommentReport`（`bizType=comment`，bizId = rpid）；
- **作者回查**：查 `CommentIndex` 取 `mid`；
- **下架**：`CommentIndex.state` 置 hidden（评论不进 Feed，无需同步 `TResourceFeed`）。

注：`reply` / `at` 由评论子系统（`app/services/comment/`）承载，此处不重复实现，
故沿用基类默认（调用时抛「该资源不支持」）。
"""

from sqlmodel import col, select

from app.models.db.comment_tbl import CommentContent, CommentIndex, CommentReport
from bili_common.models import InteractionBizTypeEnum
from app.models.enums import CommentStateEnum
from app.models.schemas.interaction import InteractionResource
from app.services.interaction_actions.base_biz import BaseBiz, biz_action
from app.services.interaction_actions.common import ops

__all__ = ["CommentBiz"]


class CommentBiz(BaseBiz):
    """评论资源。"""

    _biz_type = InteractionBizTypeEnum.COMMENT
    model = CommentReport
    error_messages = {
        "not_found": "评论不存在",
        "invalid": "参数不合法",
    }

    # ==================== 资源获取（钩子实现）====================

    async def _index(self):
        """取 CommentIndex（缓存到实例；不存在返回 None）。"""
        if getattr(self, "_index_cache", None) is None:
            self._index_cache = (
                await self.session.exec(
                    select(CommentIndex).where(col(CommentIndex.rpid) == self.biz_id)
                )
            ).one_or_none()
        return self._index_cache

    async def check_exists(self) -> bool:
        return (await self._index()) is not None

    async def _load_meta(self) -> tuple[str | None, str | None]:
        row = await self._index()
        if row is None:
            return None, None
        content = (
            await self.session.exec(
                select(CommentContent).where(col(CommentContent.rpid) == self.biz_id)
            )
        ).one_or_none()
        return (content.message if content else None), None

    async def _load_author_mid(self) -> int | None:
        row = await self._index()
        return int(row.mid) if row is not None else None

    # ==================== 举报 ====================

    @biz_action()
    async def report(self, reason_type, reason_desc: str | None = None, pics=None):
        """举报评论（幂等）。返回 (created, triggered)。"""
        from app.models.schemas import ReportCreateReq
        from app.services.admin.report import ReportService

        return await ReportService.report(
            self.session,
            self.actor_mid,
            ReportCreateReq(
                bizType=InteractionBizTypeEnum.COMMENT.value,
                bizId=self.biz_id,
                reasonType=reason_type,
                reasonDesc=reason_desc,
                pics=pics,
            ),
        )

    async def resolve_accused(self) -> int:
        """取被举报评论的作者 mid（不存在抛错）。"""
        row = (
            await self.session.exec(
                select(CommentIndex).where(col(CommentIndex.rpid) == self.biz_id)
            )
        ).one_or_none()
        if row is None:
            raise ValueError("评论不存在")
        return int(row.mid)

    async def hide(self, *, operator_mid: int = 0, **kwargs) -> None:
        """评论下架：`CommentIndex.state` 置 hidden。"""
        row = (
            await self.session.exec(
                select(CommentIndex).where(col(CommentIndex.rpid) == self.biz_id)
            )
        ).one_or_none()
        if row is not None and row.state is not CommentStateEnum.HIDDEN:
            row.state = CommentStateEnum.HIDDEN
        await self.session.commit()

    # ==================== 评论类操作（reply / at）====================

    @biz_action()
    async def reply(self, content: str, *, at_mids=None, pictures=None, emote_meta=None):
        """回复本条评论（root=biz_id）。返回 CommentAddResp。"""
        return await ops.do_comment(
            self.session, self.biz_type, self.biz_id, self.actor_mid,
            root=self.biz_id, message=content, at_mids=at_mids,
            pictures=pictures, emote_meta=emote_meta,
        )

    @biz_action()
    async def at(self, mids, content: str, *, pictures=None, emote_meta=None):
        """在本评论下 @ 提及用户（root=biz_id）。返回 CommentAddResp。"""
        return await ops.do_comment(
            self.session, self.biz_type, self.biz_id, self.actor_mid,
            root=self.biz_id, message=content, at_mids=list(mids),
            pictures=pictures, emote_meta=emote_meta,
        )
