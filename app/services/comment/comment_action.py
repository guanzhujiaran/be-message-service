"""评论互动服务：点赞 / 点踩 / 取消（Phase 2.15–2.17）。

对应 B 站评论的「赞 / 踩」能力，是评论区最热的写路径之一，必须保证：

1. **幂等**：`uq(rpid, mid)` 兜底，重复点赞不会重复计数；赞 → 踩 → 取消的状态翻转
   在同一事务内修正两个冗余计数，不会出现「赞了又取消却计数残留」。
2. **计数原子**：`like_count` / `hate_count` 一律用 `UPDATE ... SET c = c + delta`
   数据库侧自增，杜绝并发丢更新（点踩同理）。
3. **热度联动**：每次互动后增量式重算 `hot_score`，列表排序直接吃冗余列，
   禁止 `ORDER BY` 表达式（会退化成 filesort，见 Phase 2.7）。
"""

from datetime import datetime

from sqlalchemy import update
from sqlmodel import col, select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.exceptions import CommentNotInteractiveException
from app.models.db import CommentAction, CommentContent, CommentIndex
from bili_common.models import InteractionActionTypeEnum, InteractionBizTypeEnum
from app.models.enums import CommentActionEnum
from app.models.schemas import CommentActionResp, EventReportReq
from app.services.comment import VISIBLE_STATES
from app.services.user.follow import FollowService


def compute_hot_score(
    like_count: int, hate_count: int, rcount: int, created_at: datetime
) -> float:
    """热度分（Phase 3.4 工程化简化版）。

    `like - hate*1.5 + rcount*0.5 - 时间衰减`：
    - 点赞加权、点踩惩罚更重、楼中楼多说明讨论热烈加成；
    - 时间衰减让老评论自然沉底，新评论有起步优势。
    返回浮点，直接冗余进 `hot_score` 列，排序吃索引。
    """
    age_hours = max(0.0, (datetime.now() - created_at).total_seconds() / 3600.0)
    return (
        float(like_count)
        - 1.5 * float(hate_count)
        + 0.5 * float(rcount)
        - age_hours * 0.1
    )


class CommentActionService:
    """点赞 / 点踩 / 取消。"""

    @staticmethod
    async def action(
        session: AsyncSession, mid: int, rpid: int, action: CommentActionEnum
    ) -> CommentActionResp:
        """对一条评论执行点赞 / 点踩 / 取消（NONE）。

        Raises:
            CommentNotInteractiveException: 评论不存在 / 已删除 / 审核中 / 已驳回 / 已下架
                （不可对不可见评论互动，按状态给出准确反馈）。
        """
        row = (
            await session.exec(
                select(CommentIndex).where(col(CommentIndex.rpid) == rpid)
            )
        ).one_or_none()
        if row is None:
            raise CommentNotInteractiveException("评论不存在或已删除")
        if row.auditStatus not in VISIBLE_STATES:
            raise CommentNotInteractiveException.for_state(row.auditStatus)

        # ---- 拉黑拦截：评论作者与我存在任一向黑名单关系 → 禁止点赞 / 踩 ----
        # 单方面拉黑同样禁止互动：我拉黑了对方，也不能再给对方评论点赞。
        if row.mid != mid:
            rel = await FollowService.get_relation(session, mid, row.mid)
            if rel.i_blocked:
                raise CommentNotInteractiveException("你已拉黑对方，无法点赞")
            if rel.blocked_by:
                raise CommentNotInteractiveException("对方已拉黑你，无法点赞")

        prev = (
            await session.exec(
                select(CommentAction).where(
                    col(CommentAction.rpid) == rpid, col(CommentAction.mid) == mid
                )
            )
        ).one_or_none()
        prev_action = prev.action if prev else CommentActionEnum.NONE

        # ---- 计算计数增量（先撤旧、再加新）----
        d_like = 0
        d_hate = 0
        if prev_action == CommentActionEnum.LIKE:
            d_like -= 1
        elif prev_action == CommentActionEnum.HATE:
            d_hate -= 1
        if action == CommentActionEnum.LIKE:
            d_like += 1
        elif action == CommentActionEnum.HATE:
            d_hate += 1

        # ---- 互动关系幂等写入 ----
        if action == CommentActionEnum.NONE:
            if prev is not None:
                await session.delete(prev)
        else:
            if prev is None:
                session.add(CommentAction(rpid=rpid, mid=mid, action=action))
            else:
                prev.action = action
                session.add(prev)

        # ---- 计数数据库侧原子自增 ----
        values: dict = {}
        if d_like:
            values["like_count"] = col(CommentIndex.like_count) + d_like
        if d_hate:
            values["hate_count"] = col(CommentIndex.hate_count) + d_hate
        if values:
            await session.exec(  # type: ignore[call-overload]
                update(CommentIndex)
                .where(col(CommentIndex.rpid) == rpid)
                .values(**values)
            )

        # ---- 热度增量重算（读回最新计数，避免内存值滞后）----
        fresh = (
            await session.exec(
                select(CommentIndex).where(col(CommentIndex.rpid) == rpid)
            )
        ).one()
        fresh.hot_score = compute_hot_score(
            fresh.like_count, fresh.hate_count, fresh.rcount, fresh.created_at
        )
        session.add(fresh)
        await session.commit()

        # ---- 弱依赖：被点赞通知（失败只告警，不影响点赞结果）----
        if action == CommentActionEnum.LIKE and fresh.mid != mid:
            await CommentActionService._notify_like(mid, fresh)

        return CommentActionResp(
            rpid=str(rpid),
            action=action,
            like_count=fresh.like_count,
            hate_count=fresh.hate_count,
        )

    @staticmethod
    async def _notify_like(actor_mid: int, row: CommentIndex) -> None:
        """弱依赖：通知被点赞评论的作者。

        独立会话投递：点赞主事务已提交，事件落库失败也不回滚点赞结果。
        """
        from app.core.database import new_session
        from app.services.message.insite.events import report_event_weakly

        async with new_session() as ns:
            content = (
                await ns.exec(
                    select(CommentContent).where(
                        col(CommentContent.rpid) == row.rpid
                    )
                )
            ).one_or_none()
        await report_event_weakly(
            EventReportReq(
                mid=row.mid,
                event_type=InteractionActionTypeEnum.LIKE,
                source_type=InteractionBizTypeEnum.COMMENT,
                source_id=str(row.oid),
                actor_mid=actor_mid,
                content=content.message if content else None,
                biz_id=str(row.rpid),
            )
        )


__all__ = ["CommentActionService", "compute_hot_score"]
