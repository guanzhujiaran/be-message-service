"""定时任务定义与调度器生命周期。

任务清单：

| 任务                     | 间隔                                | 作用                                     |
| ------------------------ | ----------------------------------- | ---------------------------------------- |
| `dispatch_notify_job`    | `notify_dispatch_interval_seconds`  | 标记已到发布时间的系统通知为「已投递」   |
| `retry_dead_letter_job`  | 5 分钟                              | 私信正文写入失败的死信补偿               |
| `prewarm_shard_job`      | 1 小时                              | 跨月时提前建好新月份的 100 张内容分表    |
| `comment_hot_score_job`  | 30 分钟                             | 评论热度分全量重算（时间衰减自然下沉）   |

说明：
- **站内信（系统通知 / 事件提醒 / 私信）的送达完全由数据库写路径保证**
  （通知读扩散、事件 / 私信写扩散），用户通过 `/pull` `/list` `/messages`
  `msg_feed` 主动拉取，**不参与任何第三方推送渠道**。因此这里不再有把站内信
  转发到 PushMe / PushPlus 的批量推送任务。
- `dispatch_notify_job` 只负责把「已到发布时间但尚未标记」的通知置为 dispatched，
  供管理端展示「已投递」状态，并防止重复扫描；通知内容本身在发布时即已对用户可见。
- **计数对账已移除（2.46.0）**：评论冗余计数（root_count/all_count/rcount/
  like_count/hate_count）的加减全部在同一数据库事务内原子 ±1，数据一致由事务保证，
  无需定时全量对账兜底（旧 `comment_reconcile_job` 全量重算持锁/占连接，
  在高并发灌数下会导致其他接口卡死，已删除）。

所有任务都用 `max_instances=1` + `coalesce=True`：小设备上任务执行时间可能
超过间隔，这两个参数保证不会堆积并发实例把机器压垮。
"""

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from loguru import logger
from sqlalchemy import and_, case, func, text, update
from sqlmodel import col, select

from app.core.config import settings
from app.core.database import new_pptr_session, new_session
from app.core.sharding import ensure_current_month_shards
from app.models.db import (
    CommentIndex,
    MomentAuthorQuality,
    TMoment,
    TInteractionStat,
    UserDeactivation,
    UserFollow,
)
from bili_common.models import InteractionBizTypeEnum
from app.models.enums import FollowStatusEnum, ResourceAuditStatusEnum
from app.models.pptr_db import PptrUserLevel
from app.services.comment import VISIBLE_STATES
from app.services.comment.comment_action import compute_hot_score
from app.services.message.dm.dm import retry_dead_letters
from app.services.message.insite.notify import NotifyService

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
        await retry_dead_letters(session)


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

    **性能约束（2.46.0）**：数据量大时严禁「全量 ORM 加载 + 一次性 commit」——
    一次提交会长时间持有大量行锁并占用连接池，拖垮高并发下的其他接口。
    改为「快照读一次算出分数 → 逐行直接 UPDATE → 每批 500 条 commit」，
    锁持有从「全量」降到「单批」，且批量 UPDATE 不依赖跨 commit 的 ORM 对象。
    """
    BATCH = 500
    try:
        async with new_session() as session:
            rows = (
                await session.exec(
                    select(CommentIndex).where(
                        CommentIndex.auditStatus.in_(VISIBLE_STATES)
                    )
                )
            ).all()
            scores = {
                r.rpid: compute_hot_score(
                    r.like_count, r.hate_count, r.rcount, r.created_at
                )
                for r in rows
            }
            items = list(scores.items())
            for i in range(0, len(items), BATCH):
                for rpid, score in items[i : i + BATCH]:
                    await session.exec(
                        update(CommentIndex)
                        .where(CommentIndex.rpid == rpid)
                        .values(hot_score=score)
                    )
                await session.commit()
        logger.info("评论热度分批量重算完成")
    except Exception as e:  # noqa: BLE001
        logger.error(f"评论热度分重算失败: {e}")


# ==================== 作者质量聚合（2.35.0 EdgeRank）====================


async def author_quality_job() -> None:
    """全量重算作者质量聚合（2.35.0）：``moment_author_quality``。

    - ``avgEngagement`` = AVG((like+comment+repost)/max(view,1))（normal + 未软删动态）；
    - ``recentPublishCount`` = 近 7 天发布量（刷屏降权依据）；
    - ``violationCount`` = 被驳回/下架（rejected/hidden）次数（违规降权依据）。

    MVP 每 1 小时全量重建一次（数据量小，删除 + 批量插入保证幂等）。
    """
    try:
        async with new_session() as session:
            rows = (
                await session.exec(
                    select(
                        TMoment.mid,
                        func.avg(
                            (
                                TInteractionStat.likeCount
                                + TInteractionStat.commentCount
                                + TInteractionStat.repostCount
                            )
                            / case(
                                (TInteractionStat.viewCount < 1, 1),
                                else_=TInteractionStat.viewCount,
                            )
                        ).label("avg_eng"),
                        func.sum(
                            case(
                                (
                                    TMoment.pubTime
                                    >= func.date_sub(
                                        func.now(), text("INTERVAL 7 DAY")
                                    ),
                                    1,
                                ),
                                else_=0,
                            )
                        ).label("recent"),
                        func.sum(
                            case(
                                (
                                    TMoment.auditStatus.in_(
                                        (
                                            ResourceAuditStatusEnum.REJECTED,
                                            ResourceAuditStatusEnum.HIDDEN,
                                        )
                                    ),
                                    1,
                                ),
                                else_=0,
                            )
                        ).label("violation"),
                    )
                    .join(
                        TInteractionStat,
                        and_(
                            TInteractionStat.bizType == InteractionBizTypeEnum.DYNAMIC,
                            TInteractionStat.bizId == TMoment.dynId,
                        ),
                    )
                    .where(TMoment.deletedAt.is_(None))
                    .group_by(TMoment.mid)
                )
            ).all()
            if not rows:
                return
            mids = [int(m) for m, *_ in rows]
            # 2.37.0：粉丝数 ≈ 被关注数（msg_user_follow 按 target_mid COUNT，排除拉黑）
            fans_map: dict[int, int] = {}
            frows = (
                await session.exec(
                    select(UserFollow.target_mid, func.count())
                    .where(
                        col(UserFollow.target_mid).in_(mids),
                        col(UserFollow.status) == FollowStatusEnum.FOLLOWING,
                    )
                    .group_by(col(UserFollow.target_mid))
                )
            ).all()
            fans_map = {int(t): int(c) for t, c in frows}
            # 2.37.0：作者等级（pptr TUserLevel 回查，失败按 0）
            level_map: dict[int, int] = {}
            try:
                async with new_pptr_session() as ps:
                    lrows = (
                        await ps.exec(
                            select(
                                PptrUserLevel.mid, PptrUserLevel.current_level
                            ).where(col(PptrUserLevel.mid).in_(mids))
                        )
                    ).all()
                    level_map = {int(m): int(lv or 0) for m, lv in lrows}
            except Exception:  # noqa: BLE001
                logger.warning("作者等级回查失败，按 0 处理")
            # 全量重建：删除 + 批量插入（聚合表可安全重建）
            await session.exec(text("DELETE FROM moment_author_quality"))
            for mid, avg_eng, recent, violation in rows:
                mid = int(mid)
                session.add(
                    MomentAuthorQuality(
                        mid=mid,
                        avgEngagement=float(avg_eng or 0.0),
                        recentPublishCount=int(recent or 0),
                        violationCount=int(violation or 0),
                        fansCount=fans_map.get(mid, 0),
                        currentLevel=level_map.get(mid, 0),
                    )
                )
            await session.commit()
            logger.info(f"作者质量聚合完成：{len(rows)} 位作者")
    except Exception as e:  # noqa: BLE001
        logger.error(f"作者质量聚合失败: {e}")


# ==================== 用户注销到期物理删除 + Casdoor 补偿 ====================


async def deactivate_expire_job() -> None:
    """扫描冷静期到期账号：物理删除本地数据，再删 Casdoor（失败转补偿态）。

    逐用户处理、独立提交，避免大批量删除长时间持锁。跨 pptr/MySQL 无分布式事务，
    依赖各清理逻辑幂等（已无数据删除 0 行正常）。编排：
    物理删 pptr + MySQL 业务数据 → MySQL 注销记录置 pending_casdoor 提交
    → 调 Casdoor delete-user：成功删记录；失败记错误 + retry，等补偿 job。
    """
    import datetime

    from app.services.user import deactivation_service
    from app.services.user.account import PptrUser

    now = datetime.datetime.now()
    async with new_session() as session:
        due = await deactivation_service.list_due(session, now, limit=100)
    if not due:
        return

    for row in due:
        mid = int(row.mid)
        try:
            # 1. 物理删除本地（pptr 四表 + MySQL 业务数据），内部各自独立事务，幂等
            await PptrUser(mid=mid).purge_local_data()
            # 2. 置 Casdoor 待删补偿态并提交（本地删除结果先落库）
            async with new_session() as s:
                fresh = await deactivation_service.get_cooling(s, mid)
                if fresh is not None:
                    await deactivation_service.mark_pending_casdoor(s, fresh)
                    await s.commit()
        except Exception as e:  # noqa: BLE001 - 单用户失败不影响整批
            logger.error(f"[deactivate-expire] 用户 {mid} 本地物理删除失败: {e}")
            continue

        # 3. 调 Casdoor 删除（最终一致，失败落补偿态，不回滚本地）
        await _delete_casdoor_with_compensation(mid, row.user_name)

    logger.info(f"[deactivate-expire] 本轮处理到期注销 {len(due)} 个")


async def casdoor_delete_retry_job() -> None:
    """补偿本地已删但 Casdoor 删除失败的账号（pending_casdoor）。"""
    from app.services.user import deactivation_service

    async with new_session() as session:
        pending = await deactivation_service.list_pending_casdoor(session, limit=100)
    if not pending:
        return
    for row in pending:
        await _delete_casdoor_with_compensation(int(row.mid), row.user_name, row)
    logger.info(f"[deactivate-expire] Casdoor 删除补偿扫描 {len(pending)} 个")


async def _delete_casdoor_with_compensation(
    mid: int,
    user_name: str | None,
    row: UserDeactivation | None = None,
) -> None:
    """调 Casdoor 删除并按结果维护注销记录；成功删记录，失败落错误态。

    超过 ``casdoor_delete_max_retry`` 后保持 ERROR 告警（不再静默吞掉）。
    """
    from app.services.user import casdoor_service, deactivation_service

    try:
        await casdoor_service.delete_casdoor_user(user_name or "")
    except Exception as e:  # noqa: BLE001 - Casdoor 不可用：记录待补偿
        async with new_session() as s:
            target = row
            if target is None:
                targets = await deactivation_service.list_pending_casdoor(s, limit=100)
                target = next((r for r in targets if int(r.mid) == mid), None)
            if target is not None:
                await deactivation_service.mark_casdoor_error(s, target, str(e))
                await s.commit()
                over_limit = (target.retry_count or 0) >= settings.casdoor_delete_max_retry
            else:
                over_limit = False
        if over_limit:
            logger.error(
                f"[deactivate-expire] 用户 {mid} Casdoor 删除重试已超上限，需人工介入: {e}"
            )
        else:
            logger.warning(f"[deactivate-expire] 用户 {mid} Casdoor 删除失败，待补偿: {e}")
        return

    # 删除成功：清除注销记录
    async with new_session() as s:
        targets = await deactivation_service.list_pending_casdoor(s, limit=100)
        target = next((r for r in targets if int(r.mid) == mid), None)
        if target is not None:
            await deactivation_service.clear_after_casdoor_deleted(s, target)
            await s.commit()
    logger.info(f"[deactivate-expire] 用户 {mid} Casdoor 账号已删除")


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
        deactivate_expire_job,
        "interval",
        seconds=settings.deactivate_expire_scan_seconds,
        id="deactivate_expire",
        **common,
    )
    scheduler.add_job(
        casdoor_delete_retry_job,
        "interval",
        seconds=settings.deactivate_expire_scan_seconds,
        id="casdoor_delete_retry",
        **common,
    )
    scheduler.start()
    logger.info(
        "后台定时任务已启动："
        f"通知投递标记 {settings.notify_dispatch_interval_seconds}s / "
        "死信补偿 300s / 分片预热 3600s / "
        "评论热度重算 1800s / "
        f"注销到期删除 {settings.deactivate_expire_scan_seconds}s"
    )


def shutdown_scheduler() -> None:
    """关闭调度器（应用退出时调用）。"""
    if scheduler.running:
        scheduler.shutdown(wait=False)
        logger.info("后台定时任务已停止")


__all__ = [
    "casdoor_delete_retry_job",
    "comment_hot_score_job",
    "deactivate_expire_job",
    "dispatch_notify_job",
    "prewarm_shard_job",
    "retry_dead_letter_job",
    "scheduler",
    "shutdown_scheduler",
    "start_scheduler",
]
