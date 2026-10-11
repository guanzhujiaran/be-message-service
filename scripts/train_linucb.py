#!/usr/bin/env python3
"""离线回放训练 LinUCB 臂参数（2.66.0）。

在线只做 UCB 打分（只读 ``TFeedBanditArm``），A/b 由本脚本**回放历史事件**重算后
整行覆盖——请求路径不写库，避免事务膨胀与锁竞争。

回放逻辑（标准 LinUCB 更新）：

    每次曝光：A += x xᵀ，b += r·x
    r = 该次曝光的奖励（0 无互动 / 1 点赞 / 2 评论 / 3 转发收藏，做归一化到 [0,1]）

样本来自 ``TFeedEventLog`` 的 impression + 曝光后 ``ml_label_window_hours`` 内的互动
（与 ``scripts/build_feed_samples.py`` 同标签口径）。

用法：
    uv run python scripts/train_linucb.py
    uv run python scripts/train_linucb.py --days 30 --dry-run
"""

import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent))

import argparse
import asyncio
from datetime import datetime

import numpy as np
from loguru import logger
from sqlmodel import col, select, text

from app.core.config import settings
from app.core.database import new_session
from app.models.db import TFeedBanditArm
from app.services.moment.linucb import (
    ArmContext,
    context_vector,
    feature_dim,
)

#: 每个资源的上下文取「最近一次已知内容特征」；特征来自 TMoment + TInteractionStat
_CONTEXT_SQL_TEMPLATE = """
SELECT
    m.dynId,
    CHAR_LENGTH(COALESCE(m.contentText, ''))              AS text_len,
    TIMESTAMPDIFF(HOUR, m.pubTime, NOW())                 AS age_hours,
    COALESCE(s.likeCount, 0)                              AS like_count,
    COALESCE(s.commentCount, 0)                           AS comment_count,
    COALESCE(s.repostCount, 0)                            AS repost_count,
    COALESCE(s.viewCount, 0)                              AS view_count,
    COALESCE(q.avgEngagement, 0)                          AS author_quality,
    MAX(CASE WHEN m.dynType = 'FORWARD' THEN 1 ELSE 0 END) AS is_forward
FROM TMoment m
LEFT JOIN TInteractionStat s ON s.bizType = 'DYNAMIC' AND s.bizId = m.dynId
LEFT JOIN moment_author_quality q ON q.mid = m.mid
WHERE m.dynId IN ({ids})
GROUP BY m.dynId, m.contentText, m.pubTime, s.likeCount, s.commentCount,
         s.repostCount, s.viewCount, q.avgEngagement
"""

_LABEL_SQL_TEMPLATE = """
SELECT e.bizId,
       CASE
           WHEN EXISTS (
               SELECT 1 FROM TResourceFavorite f
               WHERE f.bizType = 'DYNAMIC' AND f.bizId = e.bizId
                 AND f.mid = CAST(SUBSTRING_INDEX(e.viewerKey, ':', -1) AS UNSIGNED)
                 AND f.created_at >= e.created_at
                 AND f.created_at < DATE_ADD(e.created_at, INTERVAL {win} HOUR)
           ) THEN 3
           WHEN EXISTS (
               SELECT 1 FROM msg_comment_index c
               WHERE c.oid = e.bizId AND c.type = 'DYNAMIC'
                 AND c.created_at >= e.created_at
                 AND c.created_at < DATE_ADD(e.created_at, INTERVAL {win} HOUR)
           ) THEN 2
           WHEN EXISTS (
               SELECT 1 FROM TResourceLike l
               WHERE l.bizType = 'DYNAMIC' AND l.bizId = e.bizId
                 AND l.mid = CAST(SUBSTRING_INDEX(e.viewerKey, ':', -1) AS UNSIGNED)
                 AND l.created_at >= e.created_at
                 AND l.created_at < DATE_ADD(e.created_at, INTERVAL {win} HOUR)
           ) THEN 1
           ELSE 0
       END AS label
FROM TFeedEventLog e
WHERE e.event = 'IMPRESSION' AND e.bizType = 'DYNAMIC'
  AND e.created_at >= DATE_SUB(NOW(), INTERVAL {days} DAY)
"""


def _ids_clause(ids: list[int]) -> str:
    return ",".join(str(int(i)) for i in ids) or "0"


async def train(days: int, dry_run: bool) -> int:
    """回放事件重算臂参数，返回更新的臂数量。"""
    d = feature_dim()
    win = int(settings.ml_label_window_hours)
    async with new_session() as session:
        label_rows = (
            await session.exec(
                text(_LABEL_SQL_TEMPLATE.format(win=win, days=int(days)))
            )
        ).all()
        if not label_rows:
            raise SystemExit(
                "没有曝光事件：先跑通埋点（或 scripts/seed_feed_behavior.py）"
            )
        biz_ids = sorted({int(r[0]) for r in label_rows})
        ctx_rows = (
            await session.exec(
                text(_CONTEXT_SQL_TEMPLATE.format(ids=_ids_clause(biz_ids)))
            )
        ).all()

        ctx: dict[int, tuple[int, int, float, float]] = {}
        for r in ctx_rows:
            view = int(r[6] or 0)
            positive = int(r[3] or 0) + int(r[4] or 0) + int(r[5] or 0)
            ctx[int(r[0])] = (
                int(r[1] or 0),  # text_len
                float(r[2] or 0),  # age_hours
                positive / max(view, 1),  # engagement
                float(r[7] or 0),  # author_quality
            )

        # ---- 回放：A += xxᵀ, b += r·x ----
        a_map: dict[int, np.ndarray] = {}
        b_map: dict[int, np.ndarray] = {}
        counts: dict[int, int] = {}
        rewards: dict[int, float] = {}
        for biz_id, label in label_rows:
            biz_id = int(biz_id)
            if biz_id not in ctx:
                continue
            text_len, age_hours, engagement, author_quality = ctx[biz_id]
            x = context_vector(
                ArmContext(
                    is_rich=text_len >= 60,
                    is_forward=False,
                    topic_count=0,
                    text_len=text_len,
                    age_hours=age_hours,
                    engagement=engagement,
                    author_quality=author_quality,
                    is_followed=False,
                    topic_hit=0,
                )
            )
            a = a_map.setdefault(biz_id, np.eye(d))
            b = b_map.setdefault(biz_id, np.zeros(d))
            a_map[biz_id] = a + np.outer(x, x)
            b_map[biz_id] = b + (float(label) / 3.0) * x
            counts[biz_id] = counts.get(biz_id, 0) + 1
            rewards[biz_id] = rewards.get(biz_id, 0.0) + float(label)

        if dry_run:
            for biz_id in sorted(counts)[:10]:
                logger.info(
                    f"[dry-run] bizId={biz_id} 曝光={counts[biz_id]} "
                    f"奖励={rewards[biz_id]:.1f}"
                )
            return len(counts)

        now = datetime.now()
        for biz_id, a in a_map.items():
            row = (
                await session.exec(
                    select(TFeedBanditArm).where(
                        col(TFeedBanditArm.bizType) == "DYNAMIC",
                        col(TFeedBanditArm.bizId) == biz_id,
                    )
                )
            ).one_or_none()
            payload = {
                "dim": d,
                "aMatrix": a.reshape(-1).tolist(),
                "bVector": b_map[biz_id].tolist(),
                "impressionCount": counts[biz_id],
                "rewardSum": float(rewards[biz_id]),
                "updatedBy": "train_linucb",
                "lastTrainedAt": now,
                "updated_at": now,
            }
            if row is None:
                session.add(
                    TFeedBanditArm(
                        bizType="DYNAMIC",
                        bizId=biz_id,
                        created_at=now,
                        **payload,
                    )
                )
            else:
                for key, value in payload.items():
                    setattr(row, key, value)
        await session.commit()
        return len(a_map)


def main() -> None:
    parser = argparse.ArgumentParser(description="离线训练 LinUCB 臂参数（2.66.0）")
    parser.add_argument("--days", type=int, default=30, help="回看天数")
    parser.add_argument("--dry-run", action="store_true", help="只打印不写库")
    args = parser.parse_args()
    n = asyncio.run(train(args.days, args.dry_run))
    logger.info(f"done: {n} arms")


if __name__ == "__main__":
    main()
