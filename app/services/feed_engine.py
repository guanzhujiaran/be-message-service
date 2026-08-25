"""通用资源 Feed 引擎（2.36.0，承载 2.35.0 全量排序信号）。

任意资源（``bizType+bizId``）经 Provider 提供候选与计数后，统一由本引擎执行
推荐流水线（与资源类型无关）：

1. **EdgeRank 多维打分**（2.35.0）：``compute_moment_score(counts, pubTime, profile, extra)``
   ——计数键统一 ``{like, comment, repost, view, favorite, share, dislike}``，附加维度
   engagement（互动率）/ rich（内容丰富度）/ is_forward（转发惩罚）/ exposure（曝光
   冷启动）/ dislike_ratio（点踩反馈）/ author_quality + recent_publish（作者质量）；
2. **个性化 boost（登录，2.33.0 + 2.35.0 反馈闭环）**：关注作者 / 点赞过作者 /
   ``last_clicklist`` 点击作者 / 偏好话题 / 点击话题；
3. **匿名随机权重（未登录，2.34.0）**：以 ``uniq_id`` 为种子派生扰动 Profile；
4. **去重（2.32.0）**：排除 ``last_showlist``（客户端已展示资源 id）；
5. **分页**：取前 ``page_size``，``has_more`` = 排除后仍有剩余。

数据来源约定（2.36.0 计数统一）：
- 候选：``TResourceFeed``（``auditStatus='normal'`` + ``pubTime`` 窗口，统一元数据）；
- 计数：``TInteractionStat``（like/view/favorite/share/dislike/repost）+ ``CommentSubject``
  （comment 实时计数优先）；
- 资源特征（rich / is_forward）与作者质量表由 Provider 按 bizType 填充；
- 渲染：由调用方（Provider）按 ``bizType`` 分发（dynamic → ``TMoment`` 内容模块；
  其他 → RPC 详情）。
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlmodel import col, select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.core.config import settings
from app.models.db import TMoment, TMomentLike, TMomentTopicRel
from app.services.edgerank import (
    FEED_PROFILE,
    build_anon_profile,
    build_moment_counts,
    compute_moment_score,
)
from app.services.follow import FollowService


@dataclass
class FeedCandidate:
    """通用 Feed 候选（排序所需最小元数据 + 资源特征）。"""

    biz_type: str
    biz_id: int
    mid: int | None = None  # 作者 UID（2.37.0 起可空：lottery/rpa_* 等资源可能无作者）
    pub_time: datetime | None = None
    tags: list[int] = field(default_factory=list)
    # 资源特征（Provider 按 bizType 填充，供 2.35.0 附加维度使用）
    is_forward: bool = False
    rich: bool = False


async def load_personal_signals(
    session: AsyncSession,
    viewer_mid: int,
    clicklist: list[int] | None = None,
) -> dict[str, set[int]]:
    """加载推荐流个性化信号（2.33.0 + 2.35.0 反馈闭环）。

    返回 ``{follow, liked_author, topic, clicked_author, clicked_topic}``：
    - ``follow``：关注作者（``msg_user_follow``）；
    - ``liked_author`` / ``topic``：最近 ``edgerank_personalized_like_history_limit``
      条点赞历史（``TMomentLike``）批量 join ``TMoment`` / ``TMomentTopicRel``；
    - ``clicked_author`` / ``clicked_topic``：``last_clicklist``（客户端已互动列表）
      对应作者/话题（2.35.0 反馈闭环）。

    任一信号为空不影响其余（打分时缺省按 0）。
    """
    signals: dict[str, set[int]] = {
        "follow": set(),
        "liked_author": set(),
        "topic": set(),
        "clicked_author": set(),
        "clicked_topic": set(),
    }

    follow_mids = await FollowService.list_following_mids(session, viewer_mid)
    signals["follow"] = {int(m) for m in follow_mids}

    clicks = list(dict.fromkeys(clicklist or []))
    if clicks:
        ca_rows = (
            await session.exec(
                select(TMoment.mid).where(col(TMoment.dynId).in_(clicks))
            )
        ).all()
        signals["clicked_author"] = {int(r) for r in ca_rows}
        ct_rows = (
            await session.exec(
                select(TMomentTopicRel.topicId).where(
                    col(TMomentTopicRel.dynId).in_(clicks)
                )
            )
        ).all()
        signals["clicked_topic"] = {int(r) for r in ct_rows}

    limit = settings.edgerank_personalized_like_history_limit
    if limit <= 0:
        return signals

    like_rows = (
        await session.exec(
            select(TMomentLike.dynId)
            .where(col(TMomentLike.mid) == viewer_mid)
            .where(col(TMomentLike.dynId).isnot(None))
            .order_by(col(TMomentLike.created_at).desc())
            .limit(limit)
        )
    ).all()
    liked_dyn_ids = [int(r) for r in like_rows if r]
    if not liked_dyn_ids:
        return signals

    author_rows = (
        await session.exec(
            select(TMoment.mid).where(col(TMoment.dynId).in_(liked_dyn_ids))
        )
    ).all()
    signals["liked_author"] = {int(r) for r in author_rows}
    topic_rows = (
        await session.exec(
            select(TMomentTopicRel.topicId).where(
                col(TMomentTopicRel.dynId).in_(liked_dyn_ids)
            )
        )
    ).all()
    signals["topic"] = {int(r) for r in topic_rows}
    return signals


def _boost(
    c: FeedCandidate,
    signals: dict[str, set[int]],
) -> float:
    """登录用户个性化加分（2.33.0 + 2.35.0）：关注/互动作者、偏好/点击话题。"""
    b = 0.0
    if c.mid in signals.get("follow", set()):
        b += settings.edgerank_personalized_follow_weight
    if c.mid in signals.get("liked_author", set()):
        b += settings.edgerank_personalized_liked_author_weight
    if c.mid in signals.get("clicked_author", set()):
        b += settings.edgerank_click_weight
    pref_topics = signals.get("topic", set()) | signals.get("clicked_topic", set())
    if pref_topics.intersection(c.tags):
        b += settings.edgerank_personalized_topic_weight
        if signals.get("clicked_topic", set()).intersection(c.tags):
            b += settings.edgerank_click_weight
    return b


def _extra(
    c: FeedCandidate,
    counts: dict[str, int],
    author_quality: dict[int, tuple[float, int]] | None,
    report_counts: dict[int, int] | None = None,
) -> dict[str, Any]:
    """附加维度信号（2.35.0 + 2.37.0）。

    互动率 / 丰富度 / 转发 / 曝光 / 点踩 / 作者质量（含粉丝/等级）/
    举报数（``report_count``，pending 举报降权，全资源通用）。
    """
    like = int(counts.get("like", 0))
    comment = int(counts.get("comment", 0))
    repost = int(counts.get("repost", 0))
    view = int(counts.get("view", 0))
    dislike = int(counts.get("dislike", 0))
    extra: dict[str, Any] = {
        "engagement": (like + comment + repost) / max(view, 1),
        "rich": c.rich,
        "is_forward": c.is_forward,
        "exposure": view,
        "dislike_ratio": dislike / max(dislike + like, 1),
    }
    aq = author_quality.get(c.mid) if author_quality else None
    if aq:
        extra["author_quality"] = aq[0]
        extra["recent_publish"] = aq[1]
        # 2.37.0：作者粉丝/等级（旧 2 元组兼容，缺省 0）
        extra["fans"] = aq[2] if len(aq) > 2 else 0
        extra["level"] = aq[3] if len(aq) > 3 else 0
    if report_counts:
        extra["report_count"] = int(report_counts.get(c.biz_id, 0))
    return extra


async def rank_feed(
    session: AsyncSession,
    candidates: list[FeedCandidate],
    counts: dict[int, dict[str, int]],
    *,
    viewer_mid: int | None,
    last_showlist: list[int] | None,
    uniq_id: str | None,
    page_size: int,
    clicklist: list[int] | None = None,
    comment_override: dict[int, int] | None = None,
    author_quality: dict[int, tuple[float, int]] | None = None,
    report_counts: dict[int, int] | None = None,
    now: datetime | None = None,
) -> tuple[list[int], bool]:
    """通用推荐流水线，返回 ``(排序后的 biz_id 列表, has_more)``。

    Args:
        candidates: Provider 提供的候选（已按 bizType 过滤、含 pubTime/tags/特征）；
        counts: ``{biz_id: {like, comment, repost, view, favorite, share, dislike}}``；
        clicklist: 客户端已互动 biz_id 列表（2.35.0 反馈闭环）；
        comment_override: ``{biz_id: 评论系统实时计数}``（有则优先）；
        author_quality: ``{mid: (avgEngagement, recentPublishCount, fansCount, currentLevel)}``
          （作者质量表，仅 dynamic 等有聚合的 bizType 提供；2 元组兼容）；
        report_counts: ``{biz_id: pending 举报数}``（2.37.0，按 resourceType+bizId 统计，
          举报数降权，全资源通用）；
        viewer_mid: 登录用户（个性化信号）；None → 匿名（随机权重 / 全局）；
        last_showlist: 客户端已展示 biz_id 列表（去重）；
        uniq_id: 客户端唯一 ID（匿名随机种子）；
        page_size: 单页条数。
    """
    now = now or datetime.now()
    comment_override = comment_override or {}

    # 2.34.0：未登录不用全局排序——uniq_id 派生随机权重；登录走 FEED_PROFILE
    if viewer_mid is None and settings.edgerank_anon_randomize_enabled:
        profile = build_anon_profile(uniq_id)
    else:
        profile = FEED_PROFILE

    # 2.33.0 + 2.35.0：个性化信号（登录 + 开关开启）
    personalized = viewer_mid is not None and settings.edgerank_personalized_enabled
    signals: dict[str, set[int]] = {}
    if personalized:
        signals = await load_personal_signals(session, viewer_mid, clicklist)

    def _score(c: FeedCandidate) -> float:
        cm = counts.get(c.biz_id, {})
        s = compute_moment_score(
            build_moment_counts(cm, comment_override.get(c.biz_id)),
            c.pub_time,
            profile,
            now=now,
            extra=_extra(c, cm, author_quality, report_counts),
        )
        if personalized:
            s += _boost(c, signals)
        return s

    ranked = sorted(candidates, key=_score, reverse=True)

    # 2.32.0：推荐流无 page/offset 语义——排除已展示项后取前 page_size 条
    shown = set(last_showlist or ())
    if shown:
        ranked = [c for c in ranked if c.biz_id not in shown]
    page = ranked[:page_size]
    return [c.biz_id for c in page], len(ranked) > page_size


__all__ = ["FeedCandidate", "load_personal_signals", "rank_feed"]
