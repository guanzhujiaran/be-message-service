"""统一举报服务（be-message，动态 / 评论 / 用户空间 / 通用资源）。

底层复用 bili-common 的 `ReportBaseService`（通用逻辑），按 `bizType` 分发到对应子表
（`TResourceReport`=dynamic/resource / `CommentReport`=comment / `TUserReport`=user），
各表结构一致（继承 bili-common `ReportBase`），动态与评论业务分开计算（不合并计数）。

2.40.0 处置模型：**举报达阈值仅「加入审核队列」——不改变任何资源可见性**
（`_linkage` 仅返回达阈值标记），资源照常展示；所有下架/隐藏动作（动态 `hidden`、
评论 `hidden`、`TResourceFeed` 退出 Feed、RPA 归属服务处置）**只由管理员在
举报审核（`review` + `resourceAction=hide`）中显式触发**。统一幂等：一人对同一对象一次。
"""

import json

from loguru import logger
from sqlalchemy import func
from sqlmodel import col, select

from app.core.config import settings
from app.core.database import new_pptr_session, new_session
from app.models.db import TResourceFeed
from app.models.db.comment_tbl import CommentReport, CommentIndex
from app.models.db.moment_tbl import TMoment, TResourceReport
from app.models.db.report_tbl import TUserReport
from app.models.enums import (
    CommentStateEnum,
    EventTypeEnum,
    InteractionBizTypeEnum,
    MomentAuditStatusEnum,
    SourceTypeEnum,
)
from app.models.pptr_db import PptrUserInfo
from app.models.schemas import (
    EventReportReq,
    ReportCreateReq,
    ReportItem,
    ReportListResp,
    ReportReviewReq,
)
from bili_common.models.report import (
    ReportBizTypeEnum,
    ReportReasonEnum,
    ReportReviewDecisionEnum,
)
from bili_common.services.report import ReportBaseService

PIC_LIMIT = 3

# bizType → 举报子表模型（各表继承 bili-common ReportBase，结构一致）
_MODEL_MAP = {
    ReportBizTypeEnum.DYNAMIC: TResourceReport,
    ReportBizTypeEnum.COMMENT: CommentReport,
    ReportBizTypeEnum.USER: TUserReport,
    # 2.39.0：通用资源（lottery/rpa_*）举报也落 TResourceReport（resourceType 区分）
    ReportBizTypeEnum.RESOURCE: TResourceReport,
}


class ReportService:
    """统一举报服务（静态方法集合；封装 bili-common `ReportBaseService` + 业务联动）。"""

    # ==================== 统一举报入口 ====================

    @staticmethod
    async def report(
        session,
        viewer_mid: int,
        req: ReportCreateReq,
    ) -> tuple[bool, bool]:
        """统一举报（幂等：一人对同一对象只记一次）。

        Returns:
            (created, triggered)：created 是否新增；triggered 是否达阈值标记
            （仅提示"已积累 N 次举报"，**不触发任何自动处置/可见性变化**，2.40.0）。
        """
        try:
            biz = ReportBizTypeEnum(req.bizType)
        except ValueError as exc:
            raise ValueError("非法的举报来源类型") from exc
        try:
            reason = ReportReasonEnum(req.reasonType)
        except ValueError as exc:
            raise ValueError("非法的举报原因") from exc
        pics = ReportService._validate_pics(req.pics)
        model = _MODEL_MAP[biz]

        # 2.37.0：被举报对象所属资源类型（供通用举报数降权按 resourceType+bizId 统计）
        resource_type = req.resourceType
        if resource_type is not None:
            try:
                InteractionBizTypeEnum(resource_type)
            except ValueError as exc:
                raise ValueError("非法的资源类型") from exc
        elif biz is ReportBizTypeEnum.DYNAMIC:
            resource_type = int(InteractionBizTypeEnum.DYNAMIC)
        if biz is ReportBizTypeEnum.RESOURCE and resource_type is None:
            # 2.39.0：通用资源举报必须显式携带资源类型（lottery/rpa_*）
            raise ValueError("通用资源举报必须携带 resourceType")

        accused = await ReportService._resolve_accused(
            session, biz, req.bizId, resource_type=resource_type
        )
        created, _pk = await ReportBaseService.record_report(
            session,
            model,
            reporter_mid=viewer_mid,
            biz_type=biz.value,
            biz_id=req.bizId,
            resource_type=resource_type,
            accused_mid=accused,
            reason_type=int(reason),
            reason_desc=req.reasonDesc,
            pics=pics,
        )
        if not created:
            return False, False

        triggered = await ReportService._linkage(session, biz, req.bizId)
        logger.info(f"用户 {viewer_mid} 举报 {biz.value} bizId={req.bizId}")
        return True, triggered

    # ==================== 管理端 ====================

    @staticmethod
    async def list_reports(
        session,
        *,
        biz_type: ReportBizTypeEnum | None = None,
        status: str | None = None,
        page: int = 1,
        page_size: int = 20,
    ) -> ReportListResp:
        """管理端统一举报列表（biz_type / status 过滤 + 分页，按创建时间倒序）。

        `biz_type` 为空时跨三类子表汇总（各子表结构一致，逐表分页 + 内存归并）。
        """
        page = max(1, page)
        page_size = min(max(1, page_size), 50)

        if biz_type:
            try:
                biz = ReportBizTypeEnum(biz_type)
            except ValueError as exc:
                raise ValueError("非法的举报来源类型") from exc
            items, total = await ReportBaseService.list_reports(
                session,
                _MODEL_MAP[biz],
                biz_type=biz.value,
                status=status,
                page=page,
                page_size=page_size,
            )
            page_items = [ReportService._to_item(r) for r in items]
            # 2.40.0：被举报数量（同对象累计次数）
            counts = await ReportService._load_report_counts(session, page_items)
            for it in page_items:
                _c, _p = counts.get((it.bizType, it.bizId), (0, 0))
                it.reportCount = _c
                it.reportPeopleCount = _p
            return ReportListResp(
                items=page_items,
                total=total,
                page=page,
                pageSize=page_size,
            )

        # 跨三类汇总：各表分页取 (page*page_size) 再按 created_at 归并排序取一页
        merged = []
        total = 0
        for model in _MODEL_MAP.values():
            sub_items, sub_total = await ReportBaseService.list_reports(
                session, model, status=status, page=1, page_size=page * page_size
            )
            total += sub_total
            merged.extend(sub_items)
        merged.sort(key=lambda r: (r.created_at or __import__("datetime").datetime.min), reverse=True)
        page_items = merged[(page - 1) * page_size : page * page_size]
        items = [ReportService._to_item(r) for r in page_items]
        # 2.40.0：被举报数量（同对象累计次数）
        counts = await ReportService._load_report_counts(session, items)
        for it in items:
            _c, _p = counts.get((it.bizType, it.bizId), (0, 0))
            it.reportCount = _c
            it.reportPeopleCount = _p
        return ReportListResp(
            items=items,
            total=total,
            page=page,
            pageSize=page_size,
        )

    @staticmethod
    async def _load_report_counts(
        session, items: list
    ) -> dict[tuple[str, int], tuple[int, int]]:
        """按 (bizType, bizId) 聚合被举报**次数 + 人数**双口径（2.40.0）。

        口径：``(COUNT(*), COUNT(DISTINCT reportMid))``——次数为累计举报，
        人数为去重举报人（同一人多次举报只记一次）。
        TResourceReport 承载 dynamic 与 resource 两种来源，故按 bizType 分组聚合。
        """
        result: dict[tuple[str, int], tuple[int, int]] = {}
        by_model: dict[type, set[str]] = {}
        for it in items:
            model = _MODEL_MAP.get(ReportBizTypeEnum(it.bizType))
            if model is None:
                continue
            by_model.setdefault(model, set()).add(it.bizType)
        for model, biz_types in by_model.items():
            for bt in biz_types:
                ids = [it.bizId for it in items if it.bizType == bt]
                if not ids:
                    continue
                rows = (
                    await session.exec(
                        select(
                            model.bizId,
                            func.count(),
                            func.count(func.distinct(model.reportMid)),
                        )
                        .where(
                            col(model.bizType) == bt,
                            col(model.bizId).in_(ids),
                        )
                        .group_by(col(model.bizId))
                    )
                ).all()
                for b, c, p in rows:
                    result[(bt, int(b))] = (int(c), int(p))
        return result

    @staticmethod
    async def review(session, admin_mid: int, req: ReportReviewReq) -> None:
        """管理端审核举报（resolve=属实已处理 / reject=驳回）。

        2.38.0：``decision=resolve`` 且 ``resourceAction=hide`` 时，联动**处置
        被举报资源**——动态（bizType=dynamic）→ ``TMoment`` + ``TResourceFeed``
        置 hidden（退出 Feed）；评论（bizType=comment）→ ``CommentIndex.state``
        置 hidden；用户（bizType=user）预留。作者站内通知待后续。
        """
        if req.resourceAction is not None and req.resourceAction not in {"hide"}:
            raise ValueError("非法的资源处置动作")
        rec = None
        found_model = None
        for model in _MODEL_MAP.values():
            rec = (
                await session.exec(select(model).where(model.pk == req.reportPk))
            ).one_or_none()
            if rec is not None:
                found_model = model
                break
        if rec is None:
            raise ValueError("举报记录不存在")
        await ReportBaseService.review(
            session,
            found_model,
            report_pk=req.reportPk,
            admin_mid=admin_mid,
            decision=req.decision,
            remark=req.remark,
        )
        if req.decision == ReportReviewDecisionEnum.REJECT.value:
            # 2.40.0：举报不成立 → 举报记录已置 rejected（移出待处理队列），
            # 通知举报人"举报未通过"（弱依赖，失败不阻塞）
            await ReportService._notify_report_result(rec, admin_mid, resolved=False)
        elif req.decision == ReportReviewDecisionEnum.RESOLVE.value:
            # 2.40.0：举报成立 → 通知举报人"已成立并处理"；可选下架
            await ReportService._notify_report_result(rec, admin_mid, resolved=True)
            if req.resourceAction == "hide":
                await ReportService._hide_resource(session, rec, operator_mid=admin_mid)

    # ==================== 资源处置（2.38.0）====================

    @staticmethod
    async def _hide_resource(session, rec, *, operator_mid: int) -> None:
        """下架 / 隐藏被举报资源（同事务；资源已不存在时静默跳过）。

        2.38.0：动态下架后，**资源有作者（mid 非空）时**发 HIDE 站内事件通知作者
        （弱依赖独立会话投递，失败不阻塞处置）。
        """
        if rec.bizType == ReportBizTypeEnum.DYNAMIC.value:
            dyn = (
                await session.exec(
                    select(TMoment).where(col(TMoment.dynId) == rec.bizId)
                )
            ).one_or_none()
            if dyn is None or dyn.deletedAt is not None:
                return
            if dyn.auditStatus is not MomentAuditStatusEnum.HIDDEN:
                dyn.auditStatus = MomentAuditStatusEnum.HIDDEN
            # TResourceFeed 同步退出 Feed（通用元数据表）
            feed = (
                await session.exec(
                    select(TResourceFeed).where(
                        col(TResourceFeed.bizType) == InteractionBizTypeEnum.DYNAMIC,
                        col(TResourceFeed.bizId) == rec.bizId,
                    )
                )
            ).one_or_none()
            if feed is not None:
                feed.auditStatus = MomentAuditStatusEnum.HIDDEN.value
            await session.commit()
            if dyn.mid:
                await ReportService._notify_hide(operator_mid, dyn)
        elif rec.bizType == ReportBizTypeEnum.COMMENT.value:
            row = (
                await session.exec(
                    select(CommentIndex).where(col(CommentIndex.rpid) == rec.bizId)
                )
            ).one_or_none()
            if row is not None and row.state is not CommentStateEnum.HIDDEN:
                row.state = CommentStateEnum.HIDDEN
            await session.commit()
        elif rec.bizType == ReportBizTypeEnum.RESOURCE.value:
            # 2.39.0：通用资源（lottery/rpa_*）——双层处置：
            # 本地 TResourceFeed 退出 Feed（立即可控）+ RPC 通知归属服务实际下架（弱依赖）
            await ReportService._hide_remote_resource(
                session, rec, operator_mid=operator_mid
            )
        # user：预留（禁言/封禁待后续）

    @staticmethod
    async def _hide_remote_resource(session, rec, *, operator_mid: int) -> None:
        """处置非动态资源（lottery/rpa_*，2.39.0）。

        1) **Feed 层（本地）**：`TResourceFeed.auditStatus='hidden'`，被举报资源
           **立即退出 Feed**——任何情况下生效，与归属服务可用性无关；
        2) **资源层（RPC，弱依赖）**：调用归属服务 `hide_resource`（RPA-Browser
           按 bizType 内部路由：lottery→crawler、rpa_*→本地），资源实际下架/停用；
           RPC 失败仅告警降级（Feed 层已生效）。
        """
        try:
            bt = (
                InteractionBizTypeEnum(int(rec.resourceType))
                if rec.resourceType
                else None
            )
        except ValueError:
            bt = None
        if bt is InteractionBizTypeEnum.LOTTERY:
            # 2.40.0：lottery（crawler 资源）不允许下架——仅记录举报 + 转审核，
            # Feed 层 / RPC 均不执行（管理员处置被拒绝）。
            logger.info(f"lottery 不允许下架，跳过处置: bizId={rec.bizId}")
            return
        if bt is not None:
            feed = (
                await session.exec(
                    select(TResourceFeed).where(
                        col(TResourceFeed.bizType) == bt,
                        col(TResourceFeed.bizId) == rec.bizId,
                    )
                )
            ).one_or_none()
            if feed is not None:
                feed.auditStatus = "hidden"
        await session.commit()

        from app.services.infrastructure.rpa_rpc import rpa_rpc_client

        try:
            await rpa_rpc_client.hide_resource(
                biz_type=rec.bizType,
                biz_id=rec.bizId,
                operator_mid=operator_mid,
                reason="举报下架",
            )
        except Exception:  # noqa: BLE001
            logger.warning(
                f"举报处置 RPC 失败（资源层降级）: {rec.bizType}/{rec.bizId}"
            )

    @staticmethod
    async def _notify_hide(operator_mid: int, dyn: TMoment) -> None:
        """弱依赖：内容因举报被下架的站内事件通知作者（HIDE，2.38.0）。

        独立会话投递：即便事件落库失败，也绝不回滚举报处置主事务。
        """
        from app.services.message.insite.events import report_event_weakly

        await report_event_weakly(
            EventReportReq(
                mid=dyn.mid,
                event_type=EventTypeEnum.HIDE,
                source_type=SourceTypeEnum.DYNAMIC,
                source_id=str(dyn.dynId),
                actor_mid=operator_mid,
                content="你的内容因违规被管理员下架",
                biz_id=str(dyn.dynId),
            )
        )

    @staticmethod
    async def _notify_report_result(rec, admin_mid: int, *, resolved: bool) -> None:
        """举报审核结果通知举报人（2.40.0，弱依赖）。

        ``resolved=True`` → ``REPORT_RESOLVED``（成立已处理）；``False`` →
        ``REPORT_REJECT``（未通过）。独立会话投递，失败不阻塞举报状态流转。
        """
        from app.services.message.insite.events import report_event_weakly

        event_type = (
            EventTypeEnum.REPORT_RESOLVED if resolved else EventTypeEnum.REPORT_REJECT
        )
        content = (
            "你提交的举报已成立并处理"
            if resolved
            else "你提交的举报未通过审核"
        )
        await report_event_weakly(
            EventReportReq(
                mid=rec.reportMid,
                event_type=event_type,
                source_type=ReportService._report_source_type(rec),
                source_id=str(rec.bizId),
                actor_mid=admin_mid,
                content=content,
                biz_id=str(rec.bizId),
            )
        )

    @staticmethod
    def _report_source_type(rec) -> SourceTypeEnum:
        """举报记录 → 事件来源实体类型（按被举报对象映射）。"""
        if rec.bizType == ReportBizTypeEnum.DYNAMIC.value:
            return SourceTypeEnum.DYNAMIC
        if rec.bizType == ReportBizTypeEnum.COMMENT.value:
            return SourceTypeEnum.COMMENT
        if rec.bizType == ReportBizTypeEnum.RESOURCE.value and rec.resourceType:
            try:
                if InteractionBizTypeEnum(int(rec.resourceType)) is InteractionBizTypeEnum.LOTTERY:
                    return SourceTypeEnum.LOTTERY
            except ValueError:
                pass
        return SourceTypeEnum.OTHER

    # ==================== 内部辅助 ====================

    @staticmethod
    def _validate_pics(pics: list[str] | None) -> str | None:
        """校验图片附件：http(s) URL 列表，最多 3 张；返回 JSON 字符串。"""
        if not pics:
            return None
        if len(pics) > PIC_LIMIT:
            raise ValueError("举报图片最多 3 张")
        for u in pics:
            if not isinstance(u, str) or not (
                u.startswith("http://") or u.startswith("https://")
            ):
                raise ValueError("举报图片必须为 http(s) 链接")
        return json.dumps(pics, ensure_ascii=False)

    @staticmethod
    async def _resolve_accused(
        session, biz: ReportBizTypeEnum, biz_id: int, *, resource_type: int | None = None
    ) -> int:
        """定位被举报对象并取被举报用户 mid（biz 对象不存在则抛错）。

        ``resource_type``：仅 ``biz=RESOURCE``（通用资源举报）使用——经 RPC 详情
        取资源作者 mid；弱依赖失败返回 0（未知作者）。
        """
        if biz is ReportBizTypeEnum.RESOURCE:
            # 2.39.0：非动态资源（lottery/rpa_*）——RPC 详情取作者（弱依赖，0=未知）
            from app.services.infrastructure.rpa_rpc import rpa_rpc_client

            try:
                detail = await rpa_rpc_client.get_resource_detail(
                    InteractionBizTypeEnum(resource_type).to_text()
                    if resource_type
                    else "",
                    int(biz_id),
                )
                if detail and detail.detail and detail.detail.authorMid:
                    try:
                        return int(detail.detail.authorMid)
                    except (TypeError, ValueError):
                        return 0
            except Exception:  # noqa: BLE001
                logger.warning(f"举报资源作者回查失败: resource_type={resource_type} bizId={biz_id}")
            return 0
        if biz is ReportBizTypeEnum.DYNAMIC:
            dyn = (
                await session.exec(select(TMoment).where(col(TMoment.dynId) == biz_id))
            ).one_or_none()
            if dyn is None or dyn.deletedAt is not None:
                raise ValueError("动态不存在")
            return int(dyn.mid)
        if biz is ReportBizTypeEnum.COMMENT:
            row = (
                await session.exec(
                    select(CommentIndex).where(col(CommentIndex.rpid) == biz_id)
                )
            ).one_or_none()
            if row is None:
                raise ValueError("评论不存在")
            return int(row.mid)
        if biz is ReportBizTypeEnum.USER:
            if biz_id <= 0:
                raise ValueError("用户不存在")
            async with new_pptr_session() as ps:
                exists = (
                    await ps.exec(
                        select(PptrUserInfo.uid).where(
                            col(PptrUserInfo.uid) == int(biz_id),
                            col(PptrUserInfo.deletedAt).is_(None),
                        )
                    )
                ).first()
            if exists is None:
                raise ValueError("用户不存在")
            return int(biz_id)
        raise ValueError("非法的举报来源类型")

    @staticmethod
    async def _linkage(session, biz: ReportBizTypeEnum, biz_id: int) -> bool:
        """举报达阈值仅「加入审核队列」（2.40.0）——**不改变资源状态**。

        举报记录保持 pending（管理端举报列表即审核队列，按 status 过滤待处理）；
        资源（动态/评论/RPA/抽奖）照常展示，**是否下架由管理员在举报审核
        （``review`` + ``resourceAction=hide``）中决定**。返回 triggered =
        是否达 ``settings.report_threshold``（供前端展示"已积累 N 次举报"）。
        """
        model = _MODEL_MAP[biz]
        count = (
            await session.exec(
                select(func.count())
                .select_from(model)
                .where(
                    col(model.bizType) == biz.value,
                    col(model.bizId) == biz_id,
                )
            )
        ).one()
        return int(count or 0) >= settings.report_threshold

    @staticmethod
    def _to_item(r) -> ReportItem:
        pics = None
        if r.pics:
            try:
                pics = json.loads(r.pics)
            except (json.JSONDecodeError, TypeError):
                pics = None
        return ReportItem(
            pk=int(r.pk),
            bizType=r.bizType,
            bizId=int(r.bizId),
            accusedMid=int(r.accusedMid),
            reportMid=int(r.reportMid),
            reasonType=int(r.reasonType),
            reasonDesc=r.reasonDesc,
            pics=pics,
            auditStatus=r.auditStatus,
            auditRemark=r.auditRemark,
            auditAdminMid=int(r.auditAdminMid) if r.auditAdminMid else None,
            createdAt=r.created_at.strftime("%Y-%m-%d %H:%M:%S") if r.created_at else None,
        )


__all__ = ["ReportService"]
