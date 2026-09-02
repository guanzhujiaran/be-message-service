"""统一举报服务（be-message，**协调器**，2.48.0）。

**资源为主体**：一切与资源相关的差异（落哪张举报表 / 怎么回查被举报人 / 怎么下架）
由各资源 **BaseBiz 子类**供给（`model` / `resolve_accused()` / `hide()`），
经 `interaction_actions.base_biz.get_biz` 按 `(biz_type, biz_id)` 分发。

本模块因此退化为**纯协调器**：只编排「资源校验 → 落库 → 阈值 → 审核 → 通知」的通用流程，
不再按 `bizType` 写 `if/elif` 分流，也不再维护「bizType → 子表」的并行映射——
该映射的单一真相源是资源类的 `model` 属性（见 :func:`_model_for` / :func:`_distinct_models`）。

2.40.0 处置模型：**举报达阈值仅「加入审核队列」——不改变任何资源可见性**
（`_linkage` 仅返回达阈值标记），资源照常展示；所有下架 / 隐藏动作
（动态 hidden、评论 hidden、`TResourceFeed` 退出 Feed、RPA 归属服务处置）
**只由管理员在举报审核（`report_resolved` + `resourceAction=hide`）中显式触发**，
且**由资源自身执行**（见 `BaseBiz.report_resolved`）。统一幂等：一人对同一对象一次。
"""

import json

from loguru import logger
from sqlmodel import col, func, select

from app.core.config import settings
from bili_common.models import InteractionActionTypeEnum, InteractionBizTypeEnum
from app.models.schemas import (
    EventReportReq,
    ReportCreateReq,
    ReportItem,
    ReportListResp,
    ReportReviewReq,
)
from app.services.interaction_actions.base_biz import (
    BaseBiz,
    get_biz,
    get_biz_class,
)
from bili_common.models.report import (
    ReportReasonEnum,
    ReportReviewDecisionEnum,
)
from bili_common.services.report import ReportBaseService

PIC_LIMIT = 3


def _distinct_models() -> list[type]:
    """全部资源类涉及的**去重**举报表模型（多资源共用一张表时只算一次）。

    跨表汇总 / 按主键检索都遍历这里，避免 `TResourceReport` 被 6 个资源重复查询。
    """
    return list(dict.fromkeys(c.model for c in BaseBiz._registry.values() if c.model is not None))


def _model_for(biz_type) -> type:
    """按资源类型取其**举报记录表模型**（单一真相源 = 资源类的 `model`）。"""
    bt = InteractionBizTypeEnum.from_text(biz_type)
    model = get_biz_class(bt).model
    if model is None:
        raise ValueError(f"资源类型 {bt.to_text()} 不支持举报")
    return model


class ReportService:
    """统一举报服务（**协调器**：编排通用流程，资源差异全部下沉到各 BaseBiz 子类）。"""

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
            biz = InteractionBizTypeEnum(req.bizType)
        except ValueError as exc:
            raise ValueError("非法的举报来源类型") from exc
        try:
            reason = ReportReasonEnum(req.reasonType)
        except ValueError as exc:
            raise ValueError("非法的举报原因") from exc
        pics = ReportService._validate_pics(req.pics)
        # 资源为主体：取该资源的 BaseBiz 实例（供给落库表 + 被举报人回查 + 下架动作）
        biz_obj = get_biz(biz, session, req.bizId, viewer_mid)
        if biz_obj.model is None:
            raise ValueError(f"资源类型 {biz.to_text()} 不支持举报")
        accused = await biz_obj.resolve_accused()
        created, _pk = await ReportBaseService.record_report(
            session,
            biz_obj.model,
            reporter_mid=viewer_mid,
            biz_type=biz.value,
            biz_id=req.bizId,
            accused_mid=accused,
            reason_type=int(reason),
            reason_desc=req.reasonDesc,
            pics=pics,
        )
        if not created:
            return False, False

        triggered = await ReportService._linkage(session, biz_obj, req.bizId)
        logger.info(f"用户 {viewer_mid} 举报 {biz.value} bizId={req.bizId}")
        return True, triggered

    # ==================== 管理端 ====================

    @staticmethod
    async def list_reports(
        session,
        *,
        biz_type: InteractionBizTypeEnum | None = None,
        status: str | None = None,
        page: int = 1,
        page_size: int = 20,
    ) -> ReportListResp:
        """管理端统一举报列表（biz_type / status 过滤 + 分页，按创建时间倒序）。

        `biz_type` 为空时跨全部资源的举报表汇总（各表结构一致，逐表分页 + 内存归并）。
        """
        page = max(1, page)
        page_size = min(max(1, page_size), 50)

        if biz_type:
            try:
                biz = InteractionBizTypeEnum(biz_type)
            except ValueError as exc:
                raise ValueError("非法的举报来源类型") from exc
            model = _model_for(biz)
            items, total = await ReportBaseService.list_reports(
                session,
                model,
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

        # 跨表汇总：各表分页取 (page*page_size) 再按 created_at 归并排序取一页
        merged = []
        total = 0
        for model in ReportService._distinct_models():
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
    async def review(session, admin_mid: int, req: ReportReviewReq) -> None:
        """管理端审核举报（resolve=属实已处理 / reject=驳回）。

        2.48.0：直接委托对应资源的 BaseBiz 方法——`report_reject`（驳回）
        / `report_resolved`（成立，可选 `resourceAction=hide` 联动下架）。
        协调器不感知任何资源差异。
        """
        if req.resourceAction is not None and req.resourceAction not in {"hide"}:
            raise ValueError("非法的资源处置动作")
        rec = None
        for model in ReportService._distinct_models():
            rec = (
                await session.exec(select(model).where(model.pk == req.reportPk))
            ).one_or_none()
            if rec is not None:
                break
        if rec is None:
            raise ValueError("举报记录不存在")
        biz = get_biz(InteractionBizTypeEnum(int(rec.bizType)), session, rec.bizId, admin_mid)
        if req.decision == ReportReviewDecisionEnum.REJECT.value:
            await biz.report_reject(report_pk=req.reportPk, admin_mid=admin_mid, remark=req.remark)
        elif req.decision == ReportReviewDecisionEnum.RESOLVE.value:
            await biz.report_resolved(
                report_pk=req.reportPk,
                admin_mid=admin_mid,
                remark=req.remark,
                resource_action=req.resourceAction,
            )

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
    async def _linkage(session, biz_obj, biz_id: int) -> bool:
        """举报达阈值仅「加入审核队列」（2.40.0）——**不改变资源状态**。

        举报记录保持 pending（管理端举报列表即审核队列，按 status 过滤待处理）；
        资源照常展示，**是否下架由管理员在举报审核
        （`report_resolved` + `resourceAction=hide`）中决定**。返回 triggered =
        是否达 `settings.report_threshold`（供前端展示"已积累 N 次举报"）。
        """
        model = biz_obj.model
        count = (
            await session.exec(
                select(func.count())
                .select_from(model)
                .where(
                    col(model.bizType) == biz_obj.biz_type.value,
                    col(model.bizId) == biz_id,
                )
            )
        ).one()
        return int(count or 0) >= settings.report_threshold

    @staticmethod
    async def _load_report_counts(
        session, items: list
    ) -> dict[tuple[str, int], tuple[int, int]]:
        """按 (bizType, bizId) 聚合被举报**次数 + 人数**双口径（2.40.0）。

        口径：``(COUNT(*), COUNT(DISTINCT reportMid))``——次数为累计举报，
        人数为去重举报人（同一人多次举报只记一次）。
        多资源共用同一张表，故按 bizType 分组聚合。
        """
        result: dict[tuple[str, int], tuple[int, int]] = {}
        by_model: dict[type, set[str]] = {}
        for it in items:
            try:
                model = _model_for(it.bizType)
            except ValueError:
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
