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

    async def _load_interactable(self) -> bool | None:
        """仅 `state == NORMAL` 可互动；其余状态（删 / 未过审 / 驳回 / 下架）只可举报、不进展示。"""
        row = await self._index()
        if row is None:
            return None
        return row.state is CommentStateEnum.NORMAL

    def _build_jump_target(self, rpid: str | None = None) -> str | None:
        """评论无独立详情页：按所属**顶层资源**（`CommentIndex.type` + `oid`）拼跳转 + 楼层锚点。

        `rpid` 未传时以本条评论自身作楼层锚点（深链定位到本层）。
        """
        from app.utils.route_target import jump_target_for

        idx = getattr(self, "_index_cache", None)
        if idx is None:
            return None
        return jump_target_for(idx.type, idx.oid, rpid or self.biz_id)

    @classmethod
    async def batch_get_resources(cls, session, biz_ids, *, actor_mid=None, rpid_map=None):
        """评论批量回捞：`CommentIndex` / `CommentContent` 各一次 `IN` 查询（计划书 §5.12）。

        覆盖基类「逐条 `get_resource`」的兜底实现，避免 N+1（对齐 `DynamicBiz`）。
        装配口径与 `get_resource` 一致：`title` = 评论正文、`authorMid` = 评论作者、
        `exists` = 索引存在、`interactable` = 状态 normal、`jumpTarget` = 所属顶层资源 + 楼层锚点。
        """
        from app.utils.route_target import jump_target_for

        rpid_map = rpid_map or {}
        ids = [int(b) for b in biz_ids]
        index_map: dict[int, CommentIndex] = {}
        content_map: dict[int, CommentContent] = {}
        if ids:
            idx_rows = (
                await session.exec(
                    select(CommentIndex).where(col(CommentIndex.rpid).in_(ids))
                )
            ).all()
            index_map = {int(r.rpid): r for r in idx_rows}
            content_rows = (
                await session.exec(
                    select(CommentContent).where(col(CommentContent.rpid).in_(ids))
                )
            ).all()
            content_map = {int(r.rpid): r for r in content_rows}

        out: dict[int, InteractionResource] = {}
        for bid in ids:
            idx = index_map.get(bid)
            if idx is None:
                out[bid] = InteractionResource(
                    bizType=InteractionBizTypeEnum.COMMENT,
                    bizId=bid,
                    exists=False,
                    interactable=False,
                )
                continue
            content = content_map.get(bid)
            out[bid] = InteractionResource(
                bizType=InteractionBizTypeEnum.COMMENT,
                bizId=bid,
                authorMid=int(idx.mid),
                exists=True,
                interactable=idx.state is CommentStateEnum.NORMAL,
                title=content.message if content else None,
                cover=None,
                jumpTarget=jump_target_for(idx.type, idx.oid, rpid_map.get(bid) or bid),
            )
        return out

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
