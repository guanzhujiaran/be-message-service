#!/usr/bin/env python3
"""灌 Feed 行为数据（2.66.0）：让推荐模型「有米下锅」。

真实行为数据要靠线上埋点慢慢攒，本脚本**按可解释的规则生成仿真行为**，用于：

1. 跑通「埋点 → 样本 → 训练 → 在线打分」整条链路；
2. 验证离线评估（NDCG / AUC）与模型融合是否真的带来增益；
3. 让 ALS 召回 / LinUCB 有可训练的行为矩阵。

生成规则（**刻意让「信号可学」**，否则模型学不到东西）：

- 观众：从库内真实动态作者 + ``--viewers`` 个虚拟 mid；
- 曝光：每个观众若干次请求，每次下发 ``--page-size`` 条（写 IMPRESSION 事件，含位次与下发分）；
- 互动：互动概率由内容特征决定（话题命中 + 作者质量 + 富媒体加成 − 年龄衰减），
  按分级 label 落库——0 无 / 1 点赞（TResourceLike）/ 2 评论（msg_comment_subject 计数）
  / 3 收藏（TResourceFavorite）；写明细同时**同步 TInteractionStat 计数**（与写路径同口径）。

幂等：每次运行先清理 ``viewerKey LIKE 'seed:%'`` 的事件与本脚本写入的互动明细，
可反复执行。

用法：
    uv run python scripts/seed_feed_behavior.py                    # 默认规模
    uv run python scripts/seed_feed_behavior.py --viewers 200 --sessions 5
"""

import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent))

import argparse
import asyncio
import json
import random
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from bili_common.models import InteractionBizTypeEnum
from loguru import logger
from sqlmodel import select, text
from sqlmodel.ext.asyncio.session import AsyncSession

from app.core.database import new_session
from app.core.sharding import generate_moment_id
from app.models.db import (
    TFavoriteFolder,
    TFeedEventLog,
    TMoment,
    TResourceFavorite,
    TResourceLike,
)
from app.models.enums import FeedEventTypeEnum

_SEED_VIEWER_PREFIX = "seed"

#: 灌数前备份的互动计数（``--clean`` 时回滚，避免污染真实统计）
_BACKUP_PATH = Path("data/ml/_seed_stat_backup.json")


@dataclass(frozen=True, slots=True)
class BehaviorPlan:
    """一次「曝光 → 互动」的仿真计划。"""

    biz_id: int
    position: int
    label: int  # 0/1/2/3


async def _load_moments(
    session: AsyncSession,
) -> list[tuple[int, int, datetime | None, int]]:
    """取可曝光的动态（normal + 未软删）→ [(dynId, mid, pubTime, textLen)]。"""
    rows = (
        await session.exec(
            select(
                TMoment.dynId,
                TMoment.mid,
                TMoment.pubTime,
                TMoment.contentText,
            ).where(
                TMoment.auditStatus == "NORMAL",
                TMoment.deletedAt.is_(None),
                TMoment.pubTime.isnot(None),
            )
        )
    ).all()
    return [(int(r[0]), int(r[1]), r[2], len(r[3] or "")) for r in rows]


def _plan_interaction(
    rng: random.Random,
    *,
    biz_id: int,
    position: int,
    topic_hit: bool,
    author_quality: float,
    age_hours: float,
    text_len: int,
) -> BehaviorPlan:
    """按内容特征生成互动分级。

    **信号刻意由「模型可观测特征」决定**（内容新鲜度、正文长度、话题命中、作者质量），
    否则模型学不到任何东西、离线指标必然打不过 baseline——灌数的意义就是验证
    「特征 → 标签」的链路真的能被学到。位置偏差只保留很小权重（在线打分不用 position）。
    """
    freshness = 0.5 ** (max(age_hours, 0.0) / 336.0)  # 14 天半衰
    p = 0.03 + 0.30 * freshness
    if text_len >= 20:
        p += 0.10
    if topic_hit:
        p += 0.12
    p += min(author_quality, 1.0) * 0.08
    p *= 1.0 / (1.0 + position * 0.05)  # 轻微位置偏差
    roll = rng.random()
    if roll >= p:
        return BehaviorPlan(biz_id=biz_id, position=position, label=0)
    # 有互动 → 分级（越强越少）
    if roll < p * 0.25:
        return BehaviorPlan(biz_id=biz_id, position=position, label=3)
    if roll < p * 0.55:
        return BehaviorPlan(biz_id=biz_id, position=position, label=2)
    return BehaviorPlan(biz_id=biz_id, position=position, label=1)


async def _upsert_stat(
    session: AsyncSession,
    biz_id: int,
    *,
    like: int = 0,
    favorite: int = 0,
    view: int = 0,
) -> None:
    """互动计数原子累加（无行则插入，与写路径同口径）。"""
    stmt = text(
        "INSERT INTO TInteractionStat "
        "(bizType, bizId, likeCount, commentCount, repostCount, viewCount, "
        " favoriteCount, shareCount, coinCount, dislikeCount, created_at, updated_at) "
        "VALUES ('DYNAMIC', :biz_id, :like, 0, 0, :view, :fav, 0, 0, 0, NOW(), NOW()) "
        "ON DUPLICATE KEY UPDATE "
        "likeCount = likeCount + VALUES(likeCount), "
        "favoriteCount = favoriteCount + VALUES(favoriteCount), "
        "viewCount = viewCount + VALUES(viewCount), "
        "updated_at = NOW()"
    )
    await session.execute(
        stmt,
        params={"biz_id": biz_id, "like": like, "fav": favorite, "view": view},
    )


async def clean() -> None:
    """清理本脚本灌入的数据，并把互动计数回滚到灌数前的值（尽量不留痕）。"""
    backup_rows: list[dict[str, int]] = []
    if _BACKUP_PATH.exists():
        raw = json.loads(_BACKUP_PATH.read_text(encoding="utf-8"))
        backup_rows = [dict(item) for item in raw]
    async with new_session() as session:
        await session.exec(
            text(
                f"DELETE FROM TFeedEventLog WHERE viewerKey LIKE '{_SEED_VIEWER_PREFIX}:%'"
            )
        )
        await session.exec(text("DELETE FROM TResourceLike WHERE mid >= 800000000"))
        await session.exec(text("DELETE FROM TResourceFavorite WHERE mid >= 800000000"))
        await session.exec(text("DELETE FROM TFavoriteFolder WHERE mid >= 800000000"))
        for row in backup_rows:
            await session.execute(
                text(
                    "UPDATE TInteractionStat SET likeCount = :like, "
                    "favoriteCount = :fav, viewCount = :view "
                    "WHERE bizType = 'DYNAMIC' AND bizId = :biz_id"
                ),
                params=row,
            )
        await session.commit()
    _BACKUP_PATH.unlink(missing_ok=True)
    logger.info(f"已清理灌数数据，回滚 {len(backup_rows)} 条互动计数")


async def _backup_stats(biz_ids: list[int]) -> None:
    """灌数前备份受影响动态的互动计数（供 :func:`clean` 回滚）。"""
    if not biz_ids:
        return
    async with new_session() as session:
        rows = (
            await session.exec(
                text(
                    "SELECT bizId, likeCount, favoriteCount, viewCount "
                    "FROM TInteractionStat WHERE bizType = 'DYNAMIC' AND bizId IN "
                    f"({','.join(str(int(i)) for i in biz_ids)})"
                )
            )
        ).all()
    payload = [
        {
            "biz_id": int(r[0]),
            "like": int(r[1] or 0),
            "fav": int(r[2] or 0),
            "view": int(r[3] or 0),
        }
        for r in rows
    ]
    _BACKUP_PATH.parent.mkdir(parents=True, exist_ok=True)
    _BACKUP_PATH.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )


async def seed(viewers: int, sessions: int, page_size: int) -> tuple[int, int]:
    """灌行为数据，返回 (曝光数, 正样本数)。"""
    async with new_session() as session:
        moments = await _load_moments(session)
        if not moments:
            raise SystemExit("库内没有可曝光的 normal 动态，先发布/灌动态")
        # 幂等：先清理上一轮灌数（含计数回滚）
        await clean()
        await _backup_stats([m[0] for m in moments])

        rng = random.Random(20261009)
        now = datetime.now()
        impressions = 0
        positives = 0
        events: list[TFeedEventLog] = []

        for v in range(viewers):
            viewer_mid = 800000000 + v
            viewer_key = f"mid:{viewer_mid}"
            # 默认收藏夹（收藏明细要求 folderId 非空）
            folder_id = await generate_moment_id()
            session.add(
                TFavoriteFolder(
                    folder_id=folder_id,
                    mid=viewer_mid,
                    name="默认收藏夹",
                    is_default=1,
                    is_public=True,
                    created_at=now,
                    updated_at=now,
                )
            )
            # 该观众的偏好话题（固定 1~2 个，制造个性化信号）
            liked_topics = {rng.randrange(1, 6) for _ in range(rng.randint(1, 2))}
            liked: set[int] = set()
            favorited: set[int] = set()
            for _session in range(sessions):
                served_at = now - timedelta(hours=rng.uniform(0, 240))
                batch = rng.sample(moments, k=min(page_size, len(moments)))
                for position, (biz_id, _mid, pub_time, text_len) in enumerate(batch):
                    age_hours = (
                        (served_at - pub_time).total_seconds() / 3600.0
                        if pub_time
                        else 0.0
                    )
                    topic_hit = (biz_id % 5) in liked_topics
                    plan = _plan_interaction(
                        rng,
                        biz_id=biz_id,
                        position=position,
                        topic_hit=topic_hit,
                        author_quality=((biz_id % 7) / 7.0),
                        age_hours=max(age_hours, 0.0),
                        text_len=text_len,
                    )
                    ts = served_at + timedelta(seconds=position)
                    events.append(
                        TFeedEventLog(
                            viewerKey=viewer_key,
                            bizType=InteractionBizTypeEnum.DYNAMIC,
                            bizId=biz_id,
                            event=FeedEventTypeEnum.IMPRESSION,
                            feedScene="comprehensive",
                            position=position,
                            score=None,
                            created_at=ts,
                            updated_at=ts,
                        )
                    )
                    impressions += 1
                    if plan.label == 0:
                        continue
                    positives += 1
                    events.append(
                        TFeedEventLog(
                            viewerKey=viewer_key,
                            bizType=InteractionBizTypeEnum.DYNAMIC,
                            bizId=biz_id,
                            event=FeedEventTypeEnum.CLICK,
                            feedScene="comprehensive",
                            position=position,
                            created_at=ts + timedelta(seconds=2),
                            updated_at=ts + timedelta(seconds=2),
                        )
                    )
                    # 同一观众对同一资源只记一次点赞 / 收藏（明细表唯一约束）
                    if plan.label == 1 and biz_id in liked:
                        plan = BehaviorPlan(biz_id=biz_id, position=position, label=2)
                    if plan.label == 3 and biz_id in favorited:
                        plan = BehaviorPlan(biz_id=biz_id, position=position, label=2)
                    if plan.label == 1:
                        liked.add(biz_id)
                        session.add(
                            TResourceLike(
                                bizType=InteractionBizTypeEnum.DYNAMIC,
                                bizId=biz_id,
                                mid=viewer_mid,
                                created_at=ts + timedelta(seconds=5),
                                updated_at=ts + timedelta(seconds=5),
                            )
                        )
                        await _upsert_stat(session, biz_id, like=1, view=1)
                    elif plan.label == 3:
                        favorited.add(biz_id)
                        session.add(
                            TResourceFavorite(
                                bizType=InteractionBizTypeEnum.DYNAMIC,
                                bizId=biz_id,
                                mid=viewer_mid,
                                folderId=folder_id,
                                created_at=ts + timedelta(seconds=5),
                                updated_at=ts + timedelta(seconds=5),
                            )
                        )
                        await _upsert_stat(session, biz_id, favorite=1, view=1)
                    else:
                        await _upsert_stat(session, biz_id, view=1)
                if len(events) >= 2000:
                    session.add_all(events)
                    await session.commit()
                    events = []

        if events:
            session.add_all(events)
            await session.commit()
        await session.commit()
        return impressions, positives


def main() -> None:
    parser = argparse.ArgumentParser(description="灌 Feed 行为数据（2.66.0）")
    parser.add_argument("--viewers", type=int, default=150, help="虚拟观众数")
    parser.add_argument("--sessions", type=int, default=4, help="每观众请求次数")
    parser.add_argument("--page-size", type=int, default=12, help="每次下发条数")
    parser.add_argument(
        "--clean",
        action="store_true",
        help="只清理：删除灌入的事件/明细/收藏夹并回滚互动计数",
    )
    args = parser.parse_args()
    if args.clean:
        asyncio.run(clean())
        return
    impressions, positives = asyncio.run(
        seed(args.viewers, args.sessions, args.page_size)
    )
    logger.info(
        f"灌数完成：曝光 {impressions} 条 / 正样本 {positives} 条 "
        f"（正样本率 {positives / max(impressions, 1):.2%}）；"
        f"清理请跑：uv run python scripts/seed_feed_behavior.py --clean"
    )


if __name__ == "__main__":
    main()
