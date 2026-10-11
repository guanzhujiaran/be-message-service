#!/usr/bin/env python3
"""训练 ALS 协同召回索引（2.66.0）。

从**隐式反馈**构造「用户 × 资源」矩阵并用 ALS 分解，产出：

    data/ml/feed_als.npz  = {user_ids, item_ids, user_factors, item_factors}

反馈权重（隐式反馈强度，越大越"喜欢"）：

- 点赞 = 1.0（TResourceLike）
- 收藏 = 2.0（TResourceFavorite）
- 评论 = 1.5（msg_comment_index，按 oid 归属动态）
- 转发 = 2.0（TMoment.repostSrcDynId 指向源动态）

只保留动态资源（``bizType=DYNAMIC``）；互动数 < ``--min-interactions`` 的用户剔除
（矩阵太稀疏时 ALS 分解无意义）。

用法：
    uv run python scripts/train_als_recall.py
    uv run python scripts/train_als_recall.py --factors 64 --iterations 20
"""

import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent))

import argparse
import asyncio

import numpy as np
from loguru import logger
from sqlmodel import text

from app.core.config import settings
from app.core.database import new_session

#: 隐式反馈权重（互动类型 → 强度）
_FEEDBACK_SQL = """
SELECT mid, biz_id, weight FROM (
    SELECT mid, bizId AS biz_id, 1.0 AS weight FROM TResourceLike
    WHERE bizType = 'DYNAMIC' AND bizId IS NOT NULL
    UNION ALL
    SELECT mid, bizId AS biz_id, 2.0 AS weight FROM TResourceFavorite
    WHERE bizType = 'DYNAMIC' AND bizId IS NOT NULL
    UNION ALL
    SELECT mid, oid AS biz_id, 1.5 AS weight FROM msg_comment_index
    WHERE type = 'DYNAMIC' AND oid IS NOT NULL
    UNION ALL
    SELECT m.mid, m.repostSrcDynId AS biz_id, 2.0 AS weight FROM TMoment m
    WHERE m.repostSrcDynId IS NOT NULL AND m.deletedAt IS NULL
) t
"""


def _build_matrix(rows, min_interactions: int):
    """(mid, bizId, weight) → CSR 用户-物品矩阵 + id 映射。"""
    from scipy.sparse import csr_matrix

    user_ids = sorted({int(r[0]) for r in rows})
    item_ids = sorted({int(r[1]) for r in rows})
    i_index = {v: j for j, v in enumerate(item_ids)}
    indptr = [0]
    indices: list[int] = []
    data: list[float] = []
    by_user: dict[int, list[tuple[int, float]]] = {}
    for mid, biz_id, weight in rows:
        by_user.setdefault(int(mid), []).append((i_index[int(biz_id)], float(weight)))

    kept_users: list[int] = []
    for u in user_ids:
        cells = by_user.get(u, [])
        if len(cells) < min_interactions:
            continue
        kept_users.append(u)
        for col, w in sorted(cells):
            indices.append(col)
            data.append(w)
        indptr.append(len(indices))

    matrix = csr_matrix(
        (
            np.asarray(data, dtype=np.float32),
            np.asarray(indices, dtype=np.int32),
            np.asarray(indptr, dtype=np.int32),
        ),
        shape=(len(kept_users), len(item_ids)),
    )
    return (
        matrix,
        np.asarray(kept_users, dtype=np.int64),
        np.asarray(item_ids, dtype=np.int64),
    )


async def train(
    factors: int,
    iterations: int,
    regularization: float,
    min_interactions: int,
    out: str,
) -> int:
    """训练并保存 ALS 索引，返回参与训练的用户数。"""
    async with new_session() as session:
        rows = (await session.exec(text(_FEEDBACK_SQL))).all()
    if not rows:
        raise SystemExit("没有互动明细，无法训练 ALS（先灌数或积累真实行为）")

    matrix, user_ids, item_ids = _build_matrix(rows, min_interactions)
    if matrix.nnz == 0 or len(user_ids) == 0:
        raise SystemExit("过滤后没有可用用户，调低 --min-interactions 或增加数据")

    try:
        from implicit.als import AlternatingLeastSquares
    except ImportError as e:  # pragma: no cover - 依赖缺失时降级
        raise SystemExit("未安装 implicit：uv add implicit") from e

    model = AlternatingLeastSquares(
        factors=factors,
        regularization=regularization,
        iterations=iterations,
        random_state=42,
    )
    model.fit(matrix)

    out_path = Path(out or settings.ml_als_model_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    np.savez(
        out_path,
        user_ids=user_ids,
        item_ids=item_ids,
        user_factors=np.asarray(model.user_factors, dtype=np.float32),
        item_factors=np.asarray(model.item_factors, dtype=np.float32),
    )
    logger.info(
        f"ALS 索引已保存：{out_path}（users={len(user_ids)}, items={len(item_ids)}, "
        f"factors={factors}, nnz={matrix.nnz}）"
    )
    return len(user_ids)


def main() -> None:
    parser = argparse.ArgumentParser(description="训练 ALS 协同召回索引（2.66.0）")
    parser.add_argument(
        "--factors", type=int, default=0, help="隐向量维度（默认取 settings）"
    )
    parser.add_argument("--iterations", type=int, default=0, help="迭代轮次")
    parser.add_argument(
        "--min-interactions", type=int, default=0, help="用户最少互动数"
    )
    parser.add_argument("--out", type=str, default="", help="输出 npz 路径")
    args = parser.parse_args()
    n_users = asyncio.run(
        train(
            args.factors or settings.ml_als_factors,
            args.iterations or settings.ml_als_iterations,
            settings.ml_als_regularization,
            args.min_interactions or settings.ml_als_min_interactions,
            args.out,
        )
    )
    logger.info(f"done: {n_users} users")


if __name__ == "__main__":
    main()
