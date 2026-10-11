#!/usr/bin/env python3
"""构建推荐训练样本（2.66.0 机器学习推荐 P0）。

把「一次曝光」与曝光后 ``ml_label_window_hours`` 内的互动 join 成监督样本：

    (曝光事件 TFeedEventLog.event=IMPRESSION)
        + 该观众在窗口内对该资源的互动明细 → label
        + 内容侧 / 用户侧 / 上下文特征           → features

**label（graded relevance，供 LightGBM LambdaRank 用）**：

- 0 = 曝光后无互动；
- 1 = 点赞；
- 2 = 评论；
- 3 = 转发或收藏（最强正反馈）。

**groupId** = ``viewerKey + 曝光时间（截断到分钟）``，即「同一次请求下发的一批」
作为 LambdaRank 的一个 query group——排序模型学的是「同一批里该把谁排前面」。

产出 CSV（``data/ml/feed_samples.csv``），首列 ``groupId`` / ``label``，其余为特征列；
特征名清单同时写入 ``data/ml/feature_names.json``，供在线打分脚本按同名顺序构造向量
（离线/在线特征必须同序，这是最容易出错的地方，故落盘一份契约）。

用法：
    uv run python scripts/build_feed_samples.py                  # 全量构建
    uv run python scripts/build_feed_samples.py --days 7 --out /tmp/s.csv
"""

import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent))

import argparse
import asyncio
import csv
import json
from dataclasses import dataclass

from loguru import logger
from sqlmodel import text

from app.core.config import settings
from app.core.database import new_session

#: 特征列顺序（**契约**：离线构建与在线打分必须同序，改动需同步 ml_ranker.py）
FEATURE_NAMES: list[str] = [
    "age_hours",  # 内容年龄（小时，log1p 压缩）
    "topic_count",  # 话题数
    "is_rich",  # 是否富媒体（图片/视频/外链）
    "is_forward",  # 是否转发动态
    "text_len",  # 正文长度
    "like_count",  # 累计点赞
    "comment_count",  # 累计评论
    "repost_count",  # 累计转发
    "view_count",  # 累计曝光（浏览）
    "engagement",  # 互动率 = 正反馈 / max(view,1)
    "author_avg_engagement",  # 作者历史平均互动
    "author_recent_publish",  # 作者近 7 天发布量
    "author_fans",  # 作者粉丝数（log1p）
    "author_level",  # 作者等级
    "is_followed",  # 观众是否关注作者
    "topic_hit",  # 候选话题 ∩ 观众互动过的话题数
    "hour_of_day",  # 曝光时段（0~23）
    "position",  # 下发位次（位置偏差特征）
]

_LABEL_WINDOW_HOURS = settings.ml_label_window_hours

#: 互动明细 → label 分级（越大越强）
_LABEL_SQL = f"""
SELECT
    e.pk                        AS event_pk,
    e.viewerKey                 AS viewer_key,
    e.bizId                     AS biz_id,
    e.created_at                AS served_at,
    e.position                  AS position,
    e.score                     AS score,
    CASE
        WHEN EXISTS (
            SELECT 1 FROM TResourceFavorite f
            WHERE f.bizType = 'DYNAMIC' AND f.bizId = e.bizId
              AND f.mid = CAST(SUBSTRING_INDEX(e.viewerKey, ':', -1) AS UNSIGNED)
              AND f.created_at >= e.created_at
              AND f.created_at < DATE_ADD(e.created_at, INTERVAL {_LABEL_WINDOW_HOURS} HOUR)
        ) THEN 3
        WHEN EXISTS (
            SELECT 1 FROM TMoment m2
            WHERE m2.repostSrcDynId = e.bizId AND m2.deletedAt IS NULL
              AND m2.created_at >= e.created_at
              AND m2.created_at < DATE_ADD(e.created_at, INTERVAL {_LABEL_WINDOW_HOURS} HOUR)
        ) THEN 3
        WHEN EXISTS (
            SELECT 1 FROM msg_comment_index c
            WHERE c.oid = e.bizId AND c.type = 'DYNAMIC'
              AND c.created_at >= e.created_at
              AND c.created_at < DATE_ADD(e.created_at, INTERVAL {_LABEL_WINDOW_HOURS} HOUR)
        ) THEN 2
        WHEN EXISTS (
            SELECT 1 FROM TResourceLike l
            WHERE l.bizType = 'DYNAMIC' AND l.bizId = e.bizId
              AND l.mid = CAST(SUBSTRING_INDEX(e.viewerKey, ':', -1) AS UNSIGNED)
              AND l.created_at >= e.created_at
              AND l.created_at < DATE_ADD(e.created_at, INTERVAL {_LABEL_WINDOW_HOURS} HOUR)
        ) THEN 1
        ELSE 0
    END                         AS label
FROM TFeedEventLog e
WHERE e.event = 'IMPRESSION'
  AND e.bizType = 'DYNAMIC'
  AND e.created_at >= DATE_SUB(NOW(), INTERVAL :days DAY)
ORDER BY e.viewerKey, e.created_at
"""

#: 内容侧特征（一次性按 bizId 取回，避免 N+1）
_FEATURE_SQL = """
SELECT
    m.dynId                     AS biz_id,
    m.mid                       AS mid,
    m.dynType                   AS dyn_type,
    m.topicId                   AS topic_id,
    CHAR_LENGTH(COALESCE(m.contentText, '')) AS text_len,
    TIMESTAMPDIFF(HOUR, m.pubTime, NOW())    AS age_hours,
    COALESCE(s.likeCount, 0)    AS like_count,
    COALESCE(s.commentCount, 0) AS comment_count,
    COALESCE(s.repostCount, 0)  AS repost_count,
    COALESCE(s.viewCount, 0)    AS view_count,
    COALESCE(q.avgEngagement, 0)    AS author_avg_engagement,
    COALESCE(q.recentPublishCount, 0) AS author_recent_publish,
    COALESCE(q.fansCount, 0)    AS author_fans,
    COALESCE(q.currentLevel, 0) AS author_level
FROM TMoment m
LEFT JOIN TInteractionStat s
    ON s.bizType = 'DYNAMIC' AND s.bizId = m.dynId
LEFT JOIN moment_author_quality q
    ON q.mid = m.mid
WHERE m.dynId IN :ids
"""

_TOPIC_COUNT_SQL = """
SELECT dynId, COUNT(*) AS topic_count
FROM TMomentTopicRel
WHERE dynId IN :ids
GROUP BY dynId
"""


@dataclass(frozen=True, slots=True)
class _ContentFeatures:
    """内容侧特征（从 SQL 行转来的强类型值对象）。"""

    mid: int
    is_forward: int
    topic_count: int
    text_len: int
    age_hours: int
    like_count: int
    comment_count: int
    repost_count: int
    view_count: int
    engagement: float
    author_avg_engagement: float
    author_recent_publish: int
    author_fans: int
    author_level: float
    topics: tuple[int, ...]


def _group_id(viewer_key: str, served_at) -> str:
    """同一次请求（同一观众 + 同一分钟）作为 LambdaRank 的一个 query group。"""
    return f"{viewer_key}|{served_at.strftime('%Y%m%d%H%M')}"


async def _load_content_features(
    session, biz_ids: list[int]
) -> dict[int, _ContentFeatures]:
    """批量取内容侧特征（一次 IN 查询 + 话题计数一次，无 N+1）。"""
    if not biz_ids:
        return {}
    ids = tuple(biz_ids)
    rows = (await session.exec(text(_FEATURE_SQL.replace(":ids", _sql_ids(ids))))).all()
    topic_rows = (
        await session.exec(text(_TOPIC_COUNT_SQL.replace(":ids", _sql_ids(ids))))
    ).all()
    topic_map = {int(r[0]): int(r[1]) for r in topic_rows}

    out: dict[int, _ContentFeatures] = {}
    for r in rows:
        view = int(r[9])
        positive = int(r[6]) + int(r[7]) + int(r[8])
        out[int(r[0])] = _ContentFeatures(
            mid=int(r[1]),
            is_forward=1 if str(r[2]).upper() == "FORWARD" else 0,
            topic_count=int(topic_map.get(int(r[0]), 0)) + (1 if r[3] else 0),
            text_len=int(r[4]),
            age_hours=int(r[5] or 0),
            like_count=int(r[6]),
            comment_count=int(r[7]),
            repost_count=int(r[8]),
            view_count=view,
            engagement=positive / max(view, 1),
            author_avg_engagement=float(r[10] or 0),
            author_recent_publish=int(r[11] or 0),
            author_fans=int(r[12] or 0),
            author_level=float(r[13] or 0),
            topics=((int(r[3]),) if r[3] else ()),
        )
    return out


def _sql_ids(ids: tuple[int, ...]) -> str:
    """id 元组 → SQL IN 列表（全为 int，无注入面）。"""
    return "(" + ",".join(str(int(i)) for i in ids) + ")"


async def build(days: int, out_path: Path) -> int:
    """构建样本并写 CSV，返回样本行数。"""
    async with new_session() as session:
        label_rows = (
            await session.exec(text(_LABEL_SQL).bindparams(days=int(days)))
        ).all()
        if not label_rows:
            logger.warning("没有曝光事件，无法构建样本（先跑通 Feed 埋点）")
            return 0
        biz_ids = sorted({int(r[2]) for r in label_rows})
        content = await _load_content_features(session, biz_ids)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    with out_path.open("w", newline="", encoding="utf-8") as fp:
        writer = csv.writer(fp)
        writer.writerow(["groupId", "label", *FEATURE_NAMES])
        for _pk, viewer_key, biz_id, served_at, position, _score, label in label_rows:
            cf = content.get(int(biz_id))
            if cf is None:  # 动态已被删除 → 样本不可用（学不到可泛化的特征）
                continue
            writer.writerow(
                [
                    _group_id(str(viewer_key), served_at),
                    int(label),
                    # ---- FEATURE_NAMES 同序 ----
                    float(cf.age_hours),
                    int(cf.topic_count),
                    int(_is_rich_placeholder(cf)),
                    int(cf.is_forward),
                    int(cf.text_len),
                    int(cf.like_count),
                    int(cf.comment_count),
                    int(cf.repost_count),
                    int(cf.view_count),
                    float(cf.engagement),
                    float(cf.author_avg_engagement),
                    int(cf.author_recent_publish),
                    float(cf.author_fans),
                    float(cf.author_level),
                    0,  # is_followed（离线阶段占位，在线由 ml_ranker 填）
                    0,  # topic_hit（同上）
                    int(served_at.hour),
                    int(position or 0),
                ]
            )
            written += 1

    (out_path.parent / "feature_names.json").write_text(
        json.dumps(FEATURE_NAMES, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    logger.info(f"样本已写入 {out_path}（{written} 行）")
    return written


def _is_rich_placeholder(cf: _ContentFeatures) -> int:
    """富媒体标记：离线用「正文长度 + 话题数」近似（在线用 contentJson 精确判定）。"""
    return 1 if cf.text_len >= 60 or cf.topic_count > 0 else 0


def main() -> None:
    parser = argparse.ArgumentParser(description="构建推荐训练样本（2.66.0）")
    parser.add_argument(
        "--days", type=int, default=30, help="回看天数（默认 30 天内的曝光）"
    )
    parser.add_argument(
        "--out",
        type=str,
        default="data/ml/feed_samples.csv",
        help="输出 CSV 路径",
    )
    args = parser.parse_args()
    written = asyncio.run(build(args.days, Path(args.out)))
    logger.info(f"done: {written} samples")


if __name__ == "__main__":
    main()
