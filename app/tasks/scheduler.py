"""定时任务定义与调度器生命周期。

任务清单：

| 任务                     | 间隔                                | 作用                                     |
| ------------------------ | ----------------------------------- | ---------------------------------------- |
| `dispatch_notify_job`    | `notify_dispatch_interval_seconds`  | 标记已到发布时间的系统通知为「已投递」   |
| `retry_dead_letter_job`  | 5 分钟                              | 私信正文写入失败的死信补偿               |
| `prewarm_shard_job`      | 1 小时                              | 跨月时提前建好新月份的 100 张内容分表    |

说明：
- **站内信（系统通知 / 事件提醒 / 私信）的送达完全由数据库写路径保证**
  （通知读扩散、事件 / 私信写扩散），用户通过 `/pull` `/list` `/messages`
  `msg_feed` 主动拉取，**不参与任何第三方推送渠道**。因此这里不再有把站内信
  转发到 PushMe / PushPlus 的批量推送任务。
- `dispatch_notify_job` 只负责把「已到发布时间但尚未标记」的通知置为 dispatched，
  供管理端展示「已投递」状态，并防止重复扫描；通知内容本身在发布时即已对用户可见。

所有任务都用 `max_instances=1` + `coalesce=True`：小设备上任务执行时间可能
超过间隔，这两个参数保证不会堆积并发实例把机器压垮。
"""

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from loguru import logger
from sqlalchemy import case, func
from sqlmodel import select

from app.core.config import settings
from app.core.database import new_session
from app.core.sharding import ensure_current_month_shards
from app.models.db import CommentAction, CommentIndex, CommentSubject
from app.models.enums import CommentActionEnum, CommentStateEnum
from app.services.comment import VISIBLE_STATES
from app.services.comment_action import compute_hot_score
from app.services.dm import DmService
from app.services.notify import NotifyService

scheduler = AsyncIOScheduler(timezone="Asia/Shanghai")


# ==================== 系统通知定时标记 ====================


async def dispatch_notify_job() -> None:
    """把已到发布时间、尚未标记的通知置为 dispatched。

    系统通知是读扩散模型：发布（status=PUBLISHED）后即对所有目标用户可见，
    送达不依赖任何推送渠道。此任务仅用于维护管理端的「已投递」状态与去重，
    避免定时任务反复扫描同一批通知。
    """
    async with new_session() as session:
        rows = await NotifyService.fetch_dispatchable(session)
        if not rows:
            return
        for notify in rows:
            if notify.id is not None:
                await NotifyService.mark_dispatched(session, notify.id)
        logger.info(f"系统通知投递标记完成：{len(rows)} 条")


# ==================== 兜底补偿 ====================


async def retry_dead_letter_job() -> None:
    """重试私信正文写入失败的死信，保证内容最终一致。"""
    async with new_session() as session:
        await DmService.retry_dead_letters(session)


async def prewarm_shard_job() -> None:
    """预热当月分片，跨月时自动把新月份的 100 张表建好。"""
    try:
        await ensure_current_month_shards()
    except Exception as e:  # noqa: BLE001
        logger.error(f"预热私信内容分片失败: {e}")


# ==================== 评论区定时任务（Phase 3.4 / 5.10）====================


async def comment_hot_score_job() -> None:
    """批量重算评论热度分（Phase 3.4）。

    写互动时已经增量更新 `hot_score`，但时间衰减会随时间让老评论自然下沉，
    需要定时任务按 `like - hate*1.5 + rcount*0.5 - 时间衰减` 全量重算，
    保证排序长期稳定。仅在可见评论上计算。
    """
    try:
        async with new_session() as session:
            rows = (
                await session.exec(
                    select(CommentIndex).where(
                        CommentIndex.state.in_(VISIBLE_STATES)
                    )
                )
            ).all()
            for r in rows:
                r.hot_score = compute_hot_score(
                    r.like_count, r.hate_count, r.rcount, r.created_at
                )
                session.add(r)
            await session.commit()
        logger.info("评论热度分批量重算完成")
    except Exception as e:  # noqa: BLE001
        logger.error(f"评论热度分重算失败: {e}")


async def comment_reconcile_job() -> None:
    """计数对账（Phase 5.10）。

    写倾斜 / 并发丢更新可能导致冗余计数（root_count / all_count / rcount /
    like_count / hate_count）漂移。本任务以索引表为权威源，全量重算并回写，
    是计数最终一致性的兜底。小数据量下每小时跑一次足够。
    """
    try:
        async with new_session() as session:
            # 1) 每个评论区的 root_count / all_count
            group_rows = (
                await session.exec(
                    select(
                        CommentIndex.oid,
                        CommentIndex.type,
                        func.count().label("total"),
                        func.sum(
                            case((CommentIndex.root == 0, 1), else_=0)
                        ).label("root_total"),
                    )
                    .where(CommentIndex.state.in_(VISIBLE_STATES))
                    .group_by(CommentIndex.oid, CommentIndex.type)
                )
            ).all()
            for oid, ctype, total, root_total in group_rows:
                subj = (
                    await session.exec(
                        select(CommentSubject).where(
                            CommentSubject.oid == oid, CommentSubject.type == ctype
                        )
                    )
                ).one_or_none()
                if subj is None:
                    continue
                subj.all_count = int(total or 0)
                subj.root_count = int(root_total or 0)
                session.add(subj)
            await session.commit()

            # 2) 每个根评论的 rcount（楼中楼数）
            rcount_rows = (
                await session.exec(
                    select(CommentIndex.root, func.count())
                    .where(
                        CommentIndex.root != 0,
                        CommentIndex.state.in_(VISIBLE_STATES),
                    )
                    .group_by(CommentIndex.root)
                )
            ).all()
            for root, cnt in rcount_rows:
                root_row = (
                    await session.exec(
                        select(CommentIndex).where(CommentIndex.rpid == root)
                    )
                ).one_or_none()
                if root_row is not None:
                    root_row.rcount = int(cnt or 0)
                    session.add(root_row)
            await session.commit()

            # 3) 每个评论的 like_count / hate_count（来自互动关系表）
            action_rows = (
                await session.exec(
                    select(
                        CommentAction.rpid,
                        CommentAction.action,
                        func.count(),
                    ).group_by(CommentAction.rpid, CommentAction.action)
                )
            ).all()
            agg: dict[int, list[int]] = {}
            for rpid, act, cnt in action_rows:
                bucket = agg.setdefault(rpid, [0, 0])
                if act == CommentActionEnum.LIKE:
                    bucket[0] = int(cnt or 0)
                elif act == CommentActionEnum.HATE:
                    bucket[1] = int(cnt or 0)
            for rpid, (like_c, hate_c) in agg.items():
                idx_row = (
                    await session.exec(
                        select(CommentIndex).where(CommentIndex.rpid == rpid)
                    )
                ).one_or_none()
                if idx_row is not None:
                    idx_row.like_count = like_c
                    idx_row.hate_count = hate_c
                    session.add(idx_row)
            await session.commit()
        logger.info("评论计数对账完成")
    except Exception as e:  # noqa: BLE001
        logger.error(f"评论计数对账失败: {e}")


# ==================== 调度器生命周期 ====================


def start_scheduler() -> None:
    """注册并启动所有定时任务。"""
    if not settings.scheduler_enabled:
        logger.info("SCHEDULER_ENABLED=false，跳过后台定时任务注册")
        return
    if scheduler.running:
        return

    # misfire_grace_time=60：间隔型维护任务对触发时刻不敏感，事件循环短暂
    # 繁忙时允许最多延迟 60 秒仍执行（配合 coalesce 只补跑一次），避免默认
    # 1 秒宽限期导致的 "was missed by 0:00:0x" 噪音警告。
    common = {
        "max_instances": 1,
        "coalesce": True,
        "replace_existing": True,
        "misfire_grace_time": 60,
    }
    scheduler.add_job(
        dispatch_notify_job,
        "interval",
        seconds=settings.notify_dispatch_interval_seconds,
        id="dispatch_notify",
        **common,
    )
    scheduler.add_job(
        retry_dead_letter_job,
        "interval",
        seconds=300,
        id="retry_dead_letter",
        **common,
    )
    scheduler.add_job(
        prewarm_shard_job,
        "interval",
        seconds=3600,
        id="prewarm_shard",
        **common,
    )
    scheduler.add_job(
        comment_hot_score_job,
        "interval",
        seconds=1800,
        id="comment_hot_score",
        **common,
    )
    scheduler.add_job(
        comment_reconcile_job,
        "interval",
        seconds=600,
        id="comment_reconcile",
        **common,
    )
    scheduler.start()
    logger.info(
        "后台定时任务已启动："
        f"通知投递标记 {settings.notify_dispatch_interval_seconds}s / "
        "死信补偿 300s / 分片预热 3600s / "
        "评论热度重算 1800s / 评论计数对账 600s"
    )


def shutdown_scheduler() -> None:
    """关闭调度器（应用退出时调用）。"""
    if scheduler.running:
        scheduler.shutdown(wait=False)
        logger.info("后台定时任务已停止")


__all__ = [
    "scheduler",
    "start_scheduler",
    "shutdown_scheduler",
    "dispatch_notify_job",
    "retry_dead_letter_job",
    "prewarm_shard_job",
    "comment_hot_score_job",
    "comment_reconcile_job",
]
