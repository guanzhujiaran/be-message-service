"""统一举报服务（be-message，动态 / 评论 / 用户空间三类）。

底层复用 bili-common 的 `ReportBaseService`（通用逻辑），按 `bizType` 分发到对应子表
（`TMomentReport`=dynamic / `CommentReport`=comment / `TUserReport`=user），三表结构一致
（继承 bili-common `ReportBase`），动态与评论业务分开计算（各自达阈值转审，不合并计数）。

阈值联动：评论达 `report_threshold` 转 `CommentIndex` auditing；动态达阈值转 `TMoment`
auditing；用户空间仅记录留管理端。统一幂等：一人对同一对象一次。
"""

import json

from loguru import logger
from sqlalchemy import func
from sqlmodel import col, select

from app.core.config import settings
from app.core.database import new_pptr_session
from app.models.db.comment import CommentReport, CommentIndex
from app.models.db.moment import TMoment, TMomentReport
from app.models.db.report import TUserReport
from app.models.enums import (
    CommentStateEnum,
    MomentAuditStatusEnum,
)
from app.models.pptr_db import PptrUserInfo
from app.models.schemas import ReportCreateReq, ReportItem, ReportListResp, ReportReviewReq
from bili_common.models.report import ReportBizTypeEnum, ReportReasonEnum
from bili_common.services.report import ReportBaseService

PIC_LIMIT = 3

# bizType → 举报子表模型（三表均继承 bili-common ReportBase，结构一致）
_MODEL_MAP = {
    ReportBizTypeEnum.DYNAMIC: TMomentReport,
    ReportBizTypeEnum.COMMENT: CommentReport,
    ReportBizTypeEnum.USER: TUserReport,
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
            (created, triggered)：created 是否新增；triggered 是否触发阈值转审联动。
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

        accused = await ReportService._resolve_accused(session, biz, req.bizId)
        created, _pk = await ReportBaseService.record_report(
            session,
            model,
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

        triggered = await ReportService._linkage(session, biz, req.bizId)
        logger.info(f"用户 {viewer_mid} 举报 {biz.value} bizId={req.bizId}")
        return True, triggered

    # ==================== 管理端 ====================

    @staticmethod
    async def list_reports(
        session,
        *,
        biz_type: str | None = None,
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
            return ReportListResp(
                items=[ReportService._to_item(r) for r in items],
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
        return ReportListResp(
            items=[ReportService._to_item(r) for r in page_items],
            total=total,
            page=page,
            pageSize=page_size,
        )

    @staticmethod
    async def review(session, admin_mid: int, req: ReportReviewReq) -> None:
        """管理端审核举报（resolve=属实已处理 / reject=驳回）。"""
        rec = None
        for model in _MODEL_MAP.values():
            rec = (
                await session.exec(select(model).where(model.pk == req.reportPk))
            ).one_or_none()
            if rec is not None:
                await ReportBaseService.review(
                    session,
                    model,
                    report_pk=req.reportPk,
                    admin_mid=admin_mid,
                    decision=req.decision,
                    remark=req.remark,
                )
                return
        raise ValueError("举报记录不存在")

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
    async def _resolve_accused(session, biz: ReportBizTypeEnum, biz_id: int) -> int:
        """定位被举报对象并取被举报用户 mid（biz 对象不存在则抛错）。"""
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
        """业务分开计算：评论 / 动态达阈值转审核（互不合并计数），用户空间仅记录。"""
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
        threshold = settings.report_threshold

        if biz is ReportBizTypeEnum.COMMENT:
            row = (
                await session.exec(
                    select(CommentIndex).where(col(CommentIndex.rpid) == biz_id)
                )
            ).one_or_none()
            if (
                count >= threshold
                and row is not None
                and row.state == CommentStateEnum.NORMAL
            ):
                row.state = CommentStateEnum.AUDITING
                session.add(row)
                await session.commit()
                return True
        elif biz is ReportBizTypeEnum.DYNAMIC:
            dyn = (
                await session.exec(
                    select(TMoment).where(col(TMoment.dynId) == biz_id)
                )
            ).one_or_none()
            if (
                count >= threshold
                and dyn is not None
                and dyn.auditStatus == MomentAuditStatusEnum.NORMAL
            ):
                dyn.auditStatus = MomentAuditStatusEnum.AUDITING
                session.add(dyn)
                await session.commit()
                return True
        return False

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
