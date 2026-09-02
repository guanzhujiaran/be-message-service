"""动态（DYNAMIC）资源类（2.48.0）。

动态与其他资源的差异较大，故独立实现全部操作：

- **计数链路**：走 `MomentStatService`（`TMoment` 计数列），明细表 `dynId` 填实际 dynId；
- **转发**：是**发布类操作**——复用 `MomentPublishService.repost` 生成 FORWARD 动态，
  （与通用资源的「attach 行为计数」语义完全不同）；
- **审核**：`audit_approve` / `audit_reject`（DAC 审核员），含 AuditLog / Feed 同步 /
  源动态 repostCount 状态机 / 驳回通知；
- **下架**：`TMoment` + `TResourceFeed` 置 hidden，并通知作者（HIDE 事件）。
"""

from datetime import datetime

from loguru import logger
from sqlmodel import col, delete, select

from app.models.db import TMoment, TResourceDislike, TResourceFavorite, TResourceFeed, TResourceReport
from bili_common.models import InteractionActionTypeEnum, InteractionBizTypeEnum
from app.models.enums import (
    MomentAuditLogActionEnum,
    MomentAuditStatusEnum,
    MomentTypeEnum,
)
from app.models.schemas.interaction import InteractionResource
from app.models.schemas.moment import MomentRepostReq
from app.services.interaction_actions.base import (
    InteractionAclScopeEnum,
    InteractionRelationScopeEnum,
)
from app.services.interaction_actions.base_biz import BaseBiz, biz_action
from app.services.interaction_actions.common import ops

__all__ = ["DynamicBiz"]


class DynamicBiz(BaseBiz):
    """动态资源。"""

    _biz_type = InteractionBizTypeEnum.DYNAMIC
    model = TResourceReport
    error_messages = {
        "not_found": "动态不存在或暂不可互动",
        "invalid": "参数不合法",
        "folder_not_found": "收藏夹不存在",
    }

    # ==================== 资源获取（钩子实现，统一由基类 get_resource 装配）====================

    async def _get_moment(self):
        """取 `TMoment`（缓存到实例，同一 biz 多次访问只查一次；不存在返回 None）。"""
        if getattr(self, "_moment_cache", None) is None:
            self._moment_cache = (
                await self.session.exec(
                    select(TMoment).where(col(TMoment.dynId) == self.biz_id)
                )
            ).one_or_none()
        return self._moment_cache

    async def check_exists(self) -> bool:
        """动态存在且未软删、且 auditStatus 非 REJECTED/HIDDEN（被驳回 / 管理员下架视为不存在）。"""
        dyn = await self._get_moment()
        return dyn is not None and dyn.deletedAt is None and dyn.auditStatus not in (
            MomentAuditStatusEnum.REJECTED,
            MomentAuditStatusEnum.HIDDEN,
        )

    async def _load_meta(self) -> tuple[str | None, str | None]:
        from app.services.message.insite.events.source_meta import _first_pic

        dyn = await self._get_moment()
        if dyn is None:
            return None, None
        return dyn.contentText, _first_pic(dyn.contentJson)

    async def _load_author_mid(self) -> int | None:
        dyn = await self._get_moment()
        return int(dyn.mid) if dyn is not None else None

    async def _load_interactable(self) -> bool | None:
        dyn = await self._get_moment()
        if dyn is None:
            return None
        return dyn.auditStatus == MomentAuditStatusEnum.NORMAL

    @classmethod
    async def batch_get_resources(cls, session, biz_ids, *, actor_mid=None, rpid_map=None):
        """动态批量回捞：一次 IN 查询，按 dynId 装配快照（计划书 §5.11 / C20）。"""
        from sqlmodel import select as _select

        from app.models.schemas.interaction import InteractionResource
        from app.services.message.insite.events.source_meta import _first_pic
        from app.utils.route_target import jump_target_for
        from bili_common.models import InteractionBizTypeEnum

        rpid_map = rpid_map or {}
        moment_map: dict[int, TMoment] = {}
        if biz_ids:
            rows = (
                await session.exec(_select(TMoment).where(col(TMoment.dynId).in_(biz_ids)))
            ).all()
            moment_map = {m.dynId: m for m in rows}
        out: dict[int, InteractionResource] = {}
        for bid in biz_ids:
            dyn = moment_map.get(bid)
            exists = dyn is not None and dyn.deletedAt is None and dyn.auditStatus not in (
                MomentAuditStatusEnum.REJECTED,
                MomentAuditStatusEnum.HIDDEN,
            )
            title = cover = None
            author_mid = None
            interactable = exists
            if dyn is not None and exists:
                title = dyn.contentText
                cover = _first_pic(dyn.contentJson)
                author_mid = int(dyn.mid)
                interactable = dyn.auditStatus == MomentAuditStatusEnum.NORMAL
            out[bid] = InteractionResource(
                bizType=InteractionBizTypeEnum.DYNAMIC,
                bizId=bid,
                authorMid=author_mid,
                exists=exists,
                interactable=interactable,
                title=title,
                cover=cover,
                jumpTarget=jump_target_for(
                    InteractionBizTypeEnum.DYNAMIC, bid, rpid_map.get(bid)
                ),
            )
        return out

    # ==================== 互动操作 ====================

    @biz_action(relation=[InteractionRelationScopeEnum.NOT_BLOCKED])
    async def like(self, up: int = 1):
        """点赞 / 取消点赞；点赞成功时通知作者（LIKE 事件，弱依赖）。"""
        resource = await self.get_resource()
        is_like, count = await ops.do_like_dynamic(
            self.session, self.biz_id, self.actor_mid, up
        )
        if is_like and resource is not None and resource.authorMid:
            await self._notify_like(int(resource.authorMid))
        return is_like, count

    @biz_action()
    async def dislike(self, up: int = 1):
        """点踩 / 取消点踩（`dislikeCount` 供 EdgeRank 降权）。"""
        from app.services.moment.moment_stat import MomentStatService

        if up not in (1, 2):
            raise ValueError("up 参数不合法（1=点踩, 2=取消点踩）")
        existing = (
            await self.session.exec(
                select(TResourceDislike.pk).where(
                    col(TResourceDislike.bizType) == InteractionBizTypeEnum.DYNAMIC,
                    col(TResourceDislike.bizId) == self.biz_id,
                    col(TResourceDislike.mid) == self.actor_mid,
                )
            )
        ).first()
        if up == 1:
            if existing is not None:
                return True, await self._read_stat("dislikeCount")
            self.session.add(
                TResourceDislike(
                    bizType=InteractionBizTypeEnum.DYNAMIC,
                    bizId=self.biz_id,
                    mid=self.actor_mid,
                )
            )
            await self.session.flush()
            await MomentStatService.incr_stat(self.session, self.biz_id, "dislikeCount", 1)
            await self.session.commit()
            return True, await self._read_stat("dislikeCount")
        if existing is None:
            return False, await self._read_stat("dislikeCount")
        await self.session.exec(  # type: ignore[call-overload]
            TResourceDislike.__table__.delete().where(col(TResourceDislike.pk) == existing)
        )
        await MomentStatService.decr_stat(
            self.session, self.biz_id, "dislikeCount", floor_zero=True
        )
        await self.session.commit()
        return False, await self._read_stat("dislikeCount")

    @biz_action(relation=[InteractionRelationScopeEnum.NOT_BLOCKED])
    async def favorite(self, action: str = "add", folder_id=None):
        """收藏 / 取消收藏（多夹 + 用户去重，计数走 `MomentStatService`）。"""
        from app.services.moment.moment_stat import MomentStatService

        dyn = await self._get_moment()
        if dyn is None or dyn.deletedAt is not None:
            raise ValueError(self.error_messages["not_found"])
        biz_type = InteractionBizTypeEnum.DYNAMIC
        if action == "add":
            if folder_id is None:
                folder_id = await ops._ensure_default_folder(self.session, self.actor_mid)
            else:
                folder_id = int(folder_id)
            exists = (
                await self.session.exec(
                    select(TResourceFavorite.pk).where(
                        col(TResourceFavorite.bizType) == biz_type,
                        col(TResourceFavorite.bizId) == self.biz_id,
                        col(TResourceFavorite.folderId) == folder_id,
                    )
                )
            ).first()
            if exists is not None:
                await self.session.commit()
                return False, folder_id
            self.session.add(
                TResourceFavorite(
                    bizType=biz_type,
                    bizId=self.biz_id,
                    folderId=folder_id,
                    mid=self.actor_mid,
                )
            )
            await self.session.flush()
            already = await self._fav_other(folder_id)
            if already is None:
                await MomentStatService.incr_stat(self.session, self.biz_id, "favoriteCount", 1)
            await self.session.commit()
            return True, folder_id
        if action == "remove":
            if folder_id is None:
                raise ValueError(self.error_messages["invalid"])
            folder_id = int(folder_id)
            row = (
                await self.session.exec(
                    select(TResourceFavorite).where(
                        col(TResourceFavorite.bizType) == biz_type,
                        col(TResourceFavorite.bizId) == self.biz_id,
                        col(TResourceFavorite.folderId) == folder_id,
                        col(TResourceFavorite.mid) == self.actor_mid,
                    )
                )
            ).first()
            if row is None:
                await self.session.commit()
                return False, folder_id
            await self.session.exec(
                delete(TResourceFavorite).where(
                    col(TResourceFavorite.bizType) == biz_type,
                    col(TResourceFavorite.bizId) == self.biz_id,
                    col(TResourceFavorite.folderId) == folder_id,
                    col(TResourceFavorite.mid) == self.actor_mid,
                )
            )
            other = await self._fav_other(folder_id)
            if other is None:
                await MomentStatService.decr_stat(
                    self.session, self.biz_id, "favoriteCount", floor_zero=True
                )
            await self.session.commit()
            return True, folder_id
        raise ValueError(self.error_messages["invalid"])

    async def _fav_other(self, folder_id: int):
        """该用户在**其它**收藏夹是否也收藏了本动态（用户去重计数用）。"""
        return (
            await self.session.exec(
                select(TResourceFavorite.pk).where(
                    col(TResourceFavorite.mid) == self.actor_mid,
                    col(TResourceFavorite.bizType) == InteractionBizTypeEnum.DYNAMIC,
                    col(TResourceFavorite.bizId) == self.biz_id,
                    col(TResourceFavorite.folderId) != folder_id,
                )
            )
        ).first()

    async def _read_stat(self, field: str) -> int:
        """读取动态的某个计数字段。"""
        from app.models.db import TInteractionStat

        stat = (
            await self.session.exec(
                select(getattr(TInteractionStat, field)).where(
                    col(TInteractionStat.bizType) == InteractionBizTypeEnum.DYNAMIC,
                    col(TInteractionStat.bizId) == self.biz_id,
                )
            )
        ).first()
        return stat or 0

    @biz_action()
    async def share(self):
        """分享上报（不幂等）。返回最新 shareCount。"""
        from app.services.moment.moment_stat import MomentStatService

        await MomentStatService.incr_stat(self.session, self.biz_id, "shareCount", 1)
        await self.session.commit()
        return await self._read_stat("shareCount")

    @biz_action(relation=[InteractionRelationScopeEnum.NOT_BLOCKED])
    async def repost(self, content=None, **kwargs):
        """转发（FORWARD）：复用 `MomentPublishService.repost` 创建转发动态。"""
        from app.services.moment.moment_publish import MomentPublishService

        return await MomentPublishService.repost(
            self.session,
            self.actor_mid,
            MomentRepostReq(srcDynId=self.biz_id, content=content),
            client_ip=getattr(self, "client_ip", None),
            user_agent=getattr(self, "user_agent", None),
        )

    @biz_action(require_resource=False)
    async def view(self):
        """浏览上报（弱依赖，不校验动态存在）。"""
        from app.services.moment.moment_stat import MomentStatService

        return await MomentStatService.report_view(
            self.session, self.biz_id, self.actor_mid
        )

    # ==================== 评论类操作（reply / at）====================

    @biz_action()
    async def reply(self, content: str, *, at_mids=None, pictures=None, emote_meta=None, up_mid=0):
        """在动态下发表评论（一级，root=0）。返回 CommentAddResp。"""
        return await ops.do_comment(
            self.session, self.biz_type, self.biz_id, self.actor_mid,
            root=0, message=content, at_mids=at_mids,
            pictures=pictures, emote_meta=emote_meta, up_mid=up_mid,
        )

    @biz_action()
    async def at(self, mids, content: str, *, pictures=None, emote_meta=None, up_mid=0):
        """在动态下 @ 提及用户（一级，root=0）。返回 CommentAddResp。"""
        return await ops.do_comment(
            self.session, self.biz_type, self.biz_id, self.actor_mid,
            root=0, message=content, at_mids=list(mids),
            pictures=pictures, emote_meta=emote_meta, up_mid=up_mid,
        )

    # ==================== 通知（弱依赖）====================

    async def _notify_like(self, author_mid: int) -> None:
        """点赞通知作者（LIKE 事件，独立会话，失败不阻塞）。"""
        from app.models.schemas import EventReportReq
        from app.services.message.insite.events import report_event_weakly

        await report_event_weakly(
            EventReportReq(
                mid=author_mid,
                event_type=InteractionActionTypeEnum.LIKE,
                source_type=InteractionBizTypeEnum.DYNAMIC,
                source_id=str(self.biz_id),
                actor_mid=self.actor_mid,
                biz_id=str(self.biz_id),
            )
        )

    async def _notify_hide(self, operator_mid: int, dyn: TMoment) -> None:
        """下架通知作者（HIDE 事件，独立会话，失败不阻塞）。"""
        from app.models.schemas import EventReportReq
        from app.services.message.insite.events import report_event_weakly

        await report_event_weakly(
            EventReportReq(
                mid=dyn.mid,
                event_type=InteractionActionTypeEnum.HIDE,
                source_type=InteractionBizTypeEnum.DYNAMIC,
                source_id=str(dyn.dynId),
                actor_mid=operator_mid,
                content="你的内容因违规被管理员下架",
                biz_id=str(dyn.dynId),
            )
        )

    # ==================== 审核（DAC 审核员）====================

    @biz_action(acl=[InteractionAclScopeEnum.AUDITOR_ONLY], require_resource=False)
    async def audit_approve(self, remark: str | None = None):
        """审核通过：auditStatus→normal + pubTime=now()；写 AuditLog；FORWARD 源 +1。"""
        from app.services.moment.moment_audit import (
            _build_audit_log,
            _get_any,
            _safe_author_brief,
            _sync_resource_feed,
            _to_audit_item,
        )
        from app.services.moment.moment_stat import MomentStatService

        dyn = await _get_any(self.session, self.biz_id)
        if dyn is None:
            raise ValueError("动态不存在")
        from_status = dyn.auditStatus
        now = datetime.now()
        dyn.auditStatus = MomentAuditStatusEnum.NORMAL
        dyn.pubTime = now
        dyn.updated_at = now
        await _sync_resource_feed(
            self.session, self.biz_id, audit_status="normal", pub_time=now
        )
        # 状态机触发点①：FORWARD 且源动态存在 → 源动态 repostCount +1
        if dyn.dynType is MomentTypeEnum.FORWARD and dyn.repostSrcDynId:
            await MomentStatService.incr_repost_count(self.session, dyn.repostSrcDynId, 1)
        self.session.add(
            _build_audit_log(
                biz_type=InteractionBizTypeEnum.DYNAMIC,
                biz_id=self.biz_id,
                operator_mid=self.actor_mid,
                to_status=MomentAuditStatusEnum.NORMAL,
                action=MomentAuditLogActionEnum.APPROVE,
                from_status=from_status,
                remark=remark,
            )
        )
        await self.session.commit()
        await self.session.refresh(dyn)
        logger.info(f"管理员 {self.actor_mid} 审核通过动态 dynId={self.biz_id}")
        author = await _safe_author_brief(dyn.mid)
        return _to_audit_item(dyn, author)

    @biz_action(acl=[InteractionAclScopeEnum.AUDITOR_ONLY], require_resource=False)
    async def audit_reject(self, reject_reason: str, remark: str | None = None):
        """审核驳回：auditStatus→rejected + 写原因；写 AuditLog；通知作者；FORWARD∧before=normal 源 -1。"""
        from app.services.moment.moment_audit import (
            _build_audit_log,
            _get_any,
            _notify_reject,
            _safe_author_brief,
            _sync_resource_feed,
            _to_audit_item,
        )
        from app.services.moment.moment_stat import MomentStatService

        dyn = await _get_any(self.session, self.biz_id)
        if dyn is None:
            raise ValueError("动态不存在")
        from_status = dyn.auditStatus
        before_normal = from_status == MomentAuditStatusEnum.NORMAL
        now = datetime.now()
        dyn.auditStatus = MomentAuditStatusEnum.REJECTED
        dyn.auditRejectReason = reject_reason
        dyn.updated_at = now
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
                biz_type=InteractionBizTypeEnum.DYNAMIC,
                biz_id=self.biz_id,
                operator_mid=self.actor_mid,
                to_status=MomentAuditStatusEnum.REJECTED,
                action=MomentAuditLogActionEnum.REJECT,
                from_status=from_status,
                reject_reason=reject_reason,
                remark=remark,
            )
        )
        await self.session.commit()
        await self.session.refresh(dyn)
        logger.info(f"管理员 {self.actor_mid} 审核驳回动态 dynId={self.biz_id}：{reject_reason}")
        await _notify_reject(self.actor_mid, dyn, reject_reason)
        author = await _safe_author_brief(dyn.mid)
        return _to_audit_item(dyn, author)

    # ==================== 举报 ====================

    @biz_action()
    async def report(self, reason_type, reason_desc: str | None = None, pics=None):
        """举报动态（幂等）。返回 (created, triggered)。"""
        from app.models.schemas import ReportCreateReq
        from app.services.admin.report import ReportService

        return await ReportService.report(
            self.session,
            self.actor_mid,
            ReportCreateReq(
                bizType=InteractionBizTypeEnum.DYNAMIC.value,
                bizId=self.biz_id,
                reasonType=reason_type,
                reasonDesc=reason_desc,
                pics=pics,
            ),
        )

    async def resolve_accused(self) -> int:
        """取被举报动态的作者 mid（不存在 / 已软删抛错）。"""
        dyn = await self._get_moment()
        if dyn is None or dyn.deletedAt is not None:
            raise ValueError("动态不存在")
        return int(dyn.mid)

    async def hide(self, *, operator_mid: int = 0, **kwargs) -> None:
        """动态下架：动态 + Feed 置 hidden，并通知作者（资源已不存在时静默跳过）。"""
        dyn = await self._get_moment()
        if dyn is None or dyn.deletedAt is not None:
            return
        if dyn.auditStatus is not MomentAuditStatusEnum.HIDDEN:
            dyn.auditStatus = MomentAuditStatusEnum.HIDDEN
        feed = (
            await self.session.exec(
                select(TResourceFeed).where(
                    col(TResourceFeed.bizType) == InteractionBizTypeEnum.DYNAMIC,
                    col(TResourceFeed.bizId) == self.biz_id,
                )
            )
        ).one_or_none()
        if feed is not None:
            feed.auditStatus = MomentAuditStatusEnum.HIDDEN.value
        await self.session.commit()
        if dyn.mid:
            await self._notify_hide(operator_mid, dyn)
