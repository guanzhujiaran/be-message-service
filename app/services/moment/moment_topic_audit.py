"""话题创建审核服务（2.19.0，对齐动态审核 P6 流程）。

覆盖：
- 管理端待审核列表：auditStatus=auditing 按创建时间倒序分页。
- 审核通过：auditStatus→normal + pubTime=now()；**不发通知**。
- 审核驳回：auditStatus→rejected + 写 auditRejectReason；**发驳回事件通知给创建者**
  （EventTypeEnum.AUDIT_REJECT + SourceTypeEnum.DYNAMIC，source_id=`topic_{topicId}`）。

创建者昵称 / 头像经 ``PptrUser.get_many`` 只读回查（不冗余用户快照）。
"""

from datetime import datetime

from loguru import logger
from sqlmodel import col, func, select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.db import TMomentTopic
from app.models.enums import (
    EventTypeEnum,
    MomentTopicAuditStatusEnum,
    SourceTypeEnum,
)
from app.models.schemas.moment import (
    MomentTopicAuditItem,
    MomentTopicAuditListResp,
)
from app.services.user.account import PptrUser


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def _to_audit_item(topic: TMomentTopic, creator) -> MomentTopicAuditItem:
    """TMomentTopic → 话题审核队列卡片（creator 来自 PptrUser.get_many 结果）。"""
    return MomentTopicAuditItem(
        topicId=topic.topicId,
        creatorMid=topic.creatorMid,
        creatorName=creator.uname if creator else None,
        creatorFace=creator.avatar if creator else None,
        topicName=topic.topicName,
        topicCover=topic.topicCover,
        topicDesc=topic.topicDesc,
        createdAt=_iso(topic.created_at),
    )


async def _notify_reject(operator_mid: int, topic: TMomentTopic, reject_reason: str) -> None:
    """弱依赖：审核驳回事件通知创建者（AUDIT_REJECT）。

    独立会话投递：即便事件落库失败，也绝不回滚审核主事务。
    """
    from app.models.schemas import EventReportReq
    from app.services.message.insite.events import report_event_weakly

    await report_event_weakly(
        EventReportReq(
            mid=topic.creatorMid,
            event_type=EventTypeEnum.AUDIT_REJECT,
            source_type=SourceTypeEnum.DYNAMIC,
            source_id=f"topic_{topic.topicId}",
            actor_mid=operator_mid,
            content=reject_reason,
            biz_id=f"topic_{topic.topicId}",
        )
    )


def _new_session():
    from app.core.database import new_session

    return new_session()


class MomentTopicAuditService:
    """话题审核服务（静态方法集合，无状态）。"""

    @staticmethod
    async def pending_list(
        session: AsyncSession,
        *,
        page_num: int = 1,
        page_size: int = 20,
    ) -> MomentTopicAuditListResp:
        """管理端话题待审核列表：auditStatus=auditing，按创建时间倒序分页。"""
        page_num = max(1, page_num)
        page_size = min(max(1, page_size), 50)

        total = int(
            (
                await session.exec(
                    select(func.count())
                    .select_from(TMomentTopic)
                    .where(col(TMomentTopic.auditStatus) == MomentTopicAuditStatusEnum.AUDITING)
                )
            ).one()
            or 0
        )
        rows = (
            await session.exec(
                select(TMomentTopic)
                .where(col(TMomentTopic.auditStatus) == MomentTopicAuditStatusEnum.AUDITING)
                .order_by(col(TMomentTopic.created_at).desc())
                .offset((page_num - 1) * page_size)
                .limit(page_size)
            )
        ).all()

        mids = {r.creatorMid for r in rows if r.creatorMid}
        briefs = await PptrUser.get_many(list(mids))
        items = [_to_audit_item(r, briefs.get(r.creatorMid)) for r in rows]
        return MomentTopicAuditListResp(
            items=items, total=total, page_num=page_num, page_size=page_size
        )

    @staticmethod
    async def approve(
        session: AsyncSession,
        topic_id: int,
        *,
        operator_mid: int,
        remark: str | None = None,
    ) -> MomentTopicAuditItem:
        """审核通过：auditStatus→normal + pubTime=now()；**不发通知**。"""
        topic = (
            await session.exec(
                select(TMomentTopic).where(col(TMomentTopic.topicId) == topic_id)
            )
        ).one_or_none()
        if topic is None:
            raise ValueError("话题不存在")
        if topic.auditStatus is not MomentTopicAuditStatusEnum.AUDITING:
            raise ValueError("该话题已处理，不能重复审核")

        now = datetime.now()
        topic.auditStatus = MomentTopicAuditStatusEnum.NORMAL
        topic.pubTime = now
        topic.updated_at = now
        session.add(topic)
        await session.commit()
        await session.refresh(topic)
        logger.info(f"管理员 {operator_mid} 审核通过话题 topicId={topic_id}")
        briefs = await PptrUser.get_many([topic.creatorMid]) if topic.creatorMid else {}
        return _to_audit_item(topic, briefs.get(topic.creatorMid))

    @staticmethod
    async def reject(
        session: AsyncSession,
        topic_id: int,
        *,
        operator_mid: int,
        reject_reason: str,
        remark: str | None = None,
    ) -> MomentTopicAuditItem:
        """审核驳回：auditStatus→rejected + 写 auditRejectReason；发驳回通知给创建者。"""
        topic = (
            await session.exec(
                select(TMomentTopic).where(col(TMomentTopic.topicId) == topic_id)
            )
        ).one_or_none()
        if topic is None:
            raise ValueError("话题不存在")
        if topic.auditStatus is not MomentTopicAuditStatusEnum.AUDITING:
            raise ValueError("该话题已处理，不能重复审核")

        now = datetime.now()
        topic.auditStatus = MomentTopicAuditStatusEnum.REJECTED
        topic.auditRejectReason = reject_reason
        topic.updated_at = now
        session.add(topic)
        await session.commit()
        await session.refresh(topic)
        logger.info(f"管理员 {operator_mid} 审核驳回话题 topicId={topic_id}：{reject_reason}")
        # 弱依赖：通知创建者（独立会话，失败不影响审核结果）
        await _notify_reject(operator_mid, topic, reject_reason)
        briefs = await PptrUser.get_many([topic.creatorMid]) if topic.creatorMid else {}
        return _to_audit_item(topic, briefs.get(topic.creatorMid))


__all__ = ["MomentTopicAuditService"]
