#!/usr/bin/env python3
"""评论冗余计数校准（2.64.0，一次性 / 按需重跑）。

背景：`msg_comment_subject.root_count / all_count` 与动态类型的
`TInteractionStat.commentCount` 都是**写路径同事务原子 ±1** 维护的冗余计数，正常
运行不需要对账（2.46.0 已移除定时对账任务）。但以下场景会让计数残留、与评论行脱节：

- 评论索引 / 正文行被外部删除或迁移（如测试脚本误删、数据清理），计数仍留在
  `msg_comment_subject` 上 → 前端表现「评论数 N 但列表为空」；
- 历史脏数据、回写链路偶发遗漏。

口径（必须与 `CommentService.add` / `delete` / `CommentAdminService` 审核回写一致）：
- 只统计**对外可见**（`auditStatus ∈ VISIBLE_STATES`，当前即 `NORMAL`）的评论；
- `root_count` = 一级评论（`root = 0`）条数；`all_count` = 该评论区全部条数（含楼中楼）；
- `TInteractionStat.commentCount`（仅 `bizType = DYNAMIC`）以评论区 `all_count` 为准
  （动态 Feed 侧明确「以评论系统自身维护的冗余计数为准」）。

不动的字段：`floor_seq`（楼层发号计数器，重算会导致重号）、`state`、`top_rpid`。

用法：
    uv run python scripts/recount_comment_subject.py                 # 只预览（dry-run）
    uv run python scripts/recount_comment_subject.py --apply          # 写回
    uv run python scripts/recount_comment_subject.py --oid 143706587136 --apply

幂等：每次都按当前 `msg_comment_index` 重算，可反复执行——**若之后从备份恢复了评论
数据，再跑一次即可重新对齐**。
"""

import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
# 注入项目根目录（app / bili_common 可导入）
sys.path.insert(0, str(_HERE.parent))

import argparse
import asyncio

from loguru import logger
from sqlmodel import col, select, text

from app.core.database import new_session
from app.models.db import CommentSubject, TInteractionStat
from app.services.comment import VISIBLE_STATES
from bili_common.models import InteractionBizTypeEnum


def _visible_names() -> str:
    """把 `VISIBLE_STATES` 拼成 SQL IN 列表（枚举成员名，非用户输入，无注入面）。"""
    return ", ".join(f"'{s.name}'" for s in VISIBLE_STATES)


async def _load_truth(
    session, oid: int | None
) -> dict[tuple[int, str], tuple[int, int]]:
    """按评论索引统计真实计数：`{(oid, type成员名): (root_count, all_count)}`。"""
    sql = (
        "SELECT oid, type, SUM(root = 0), COUNT(*) "
        "FROM msg_comment_index "
        f"WHERE auditStatus IN ({_visible_names()}) "
        + ("AND oid = :oid " if oid is not None else "")
        + "GROUP BY oid, type"
    )
    params = {"oid": oid} if oid is not None else {}
    rows = (await session.exec(text(sql), params=params)).all()
    return {(int(r[0]), str(r[1])): (int(r[2] or 0), int(r[3])) for r in rows}


async def _run(apply: bool, oid: int | None) -> None:
    async with new_session() as session:
        truth = await _load_truth(session, oid)

        stmt = select(CommentSubject)
        if oid is not None:
            stmt = stmt.where(col(CommentSubject.oid) == oid)
        subjects = (await session.exec(stmt)).all()

        if not subjects:
            logger.info("没有匹配的评论区（msg_comment_subject）")
            return

        changed_subjects = 0
        changed_stats = 0
        for s in subjects:
            new_root, new_all = truth.get((int(s.oid), s.type.name), (0, 0))
            old_root, old_all = int(s.root_count or 0), int(s.all_count or 0)
            if (old_root, old_all) != (new_root, new_all):
                logger.info(
                    f"[subject] oid={s.oid} type={s.type.name} "
                    f"root_count {old_root} → {new_root}，all_count {old_all} → {new_all}"
                )
                if apply:
                    s.root_count = new_root
                    s.all_count = new_all
                    session.add(s)
                changed_subjects += 1

            # 动态类型额外回写 TInteractionStat.commentCount（以评论区计数为准）
            if s.type is InteractionBizTypeEnum.DYNAMIC:
                stat = (
                    await session.exec(
                        select(TInteractionStat).where(
                            col(TInteractionStat.bizType)
                            == InteractionBizTypeEnum.DYNAMIC,
                            col(TInteractionStat.bizId) == int(s.oid),
                        )
                    )
                ).one_or_none()
                if stat is not None and int(stat.commentCount or 0) != new_all:
                    logger.info(
                        f"[stat] bizId={s.oid} commentCount {stat.commentCount} → {new_all}"
                    )
                    if apply:
                        stat.commentCount = new_all
                        session.add(stat)
                    changed_stats += 1

        # 孤儿索引（有评论行但缺评论区行）只提示，不自动建区（建区涉及 up_mid / floor_seq）
        known = {(int(s.oid), s.type.name) for s in subjects}
        for key in [k for k in truth if k not in known]:
            logger.warning(
                f"[orphan] 索引有评论但评论区缺行：oid={key[0]} type={key[1]} 计数={truth[key]}"
            )

        if apply:
            await session.commit()
            logger.info(
                f"已校准并提交：评论区 {changed_subjects} 个，动态计数 {changed_stats} 个"
            )
        else:
            logger.info(
                f"dry-run：待校准评论区 {changed_subjects} 个，"
                f"动态计数 {changed_stats} 个（加 --apply 写回）"
            )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="评论冗余计数校准（root_count / all_count / TInteractionStat.commentCount）"
    )
    parser.add_argument("--apply", action="store_true", help="真正写回（默认仅预览）")
    parser.add_argument("--oid", type=int, default=None, help="只处理指定评论区的 oid")
    args = parser.parse_args()
    asyncio.run(_run(args.apply, args.oid))


if __name__ == "__main__":
    main()
