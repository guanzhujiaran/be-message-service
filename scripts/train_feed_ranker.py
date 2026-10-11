#!/usr/bin/env python3
"""训练 Feed 精排模型（2.66.0）：LightGBM LambdaRank。

输入：``scripts/build_feed_samples.py`` 产出的 CSV（groupId,label,<特征列>）
输出：``settings.ml_ranker_model_path``（默认 ``data/ml/feed_ranker.txt``）

要点：

- **排序目标**：``objective=lambdarank`` + ``label`` 为 graded relevance
  （0 无互动 / 1 点赞 / 2 评论 / 3 转发收藏），按 ``groupId`` 分组（同一次下发一批）；
- **划分**：按 group 整体切分（同一批的样本不跨 train/valid，避免泄漏）；
- **评估**：valid 上算 NDCG@K 与 AUC（互动与否二分类），并与「按曝光位次排」的
  baseline 对比——**训练不出增益就不该上线**；
- **样本量闸门**：正样本数 < ``--min-positives``（默认 50）时拒绝产出模型
  （数据太少训出来是噪声，覆盖线上模型反而更差）。

用法：
    uv run python scripts/train_feed_ranker.py
    uv run python scripts/train_feed_ranker.py --samples data/ml/s.csv --top-k 10
"""

import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent))

import argparse
import csv
import json
from datetime import datetime

import numpy as np
from loguru import logger

from app.core.config import settings

_LABEL_COL = 1  # CSV: groupId, label, features...
_HEAD_COLS = 2


def _load(path: Path) -> tuple[list[str], list[int], np.ndarray, list[str]]:
    """读样本 CSV → (groupId, label, 特征矩阵, 特征名)。"""
    with path.open(encoding="utf-8") as fp:
        rows = list(csv.reader(fp))
    if len(rows) <= 1:
        raise SystemExit(f"样本为空：{path}（先跑 scripts/build_feed_samples.py）")
    header = rows[0]
    feature_names = header[_HEAD_COLS:]
    groups = [r[0] for r in rows[1:]]
    labels = [int(float(r[_LABEL_COL])) for r in rows[1:]]
    matrix = np.asarray(
        [[float(x) for x in r[_HEAD_COLS:]] for r in rows[1:]], dtype=np.float64
    )
    logger.info(
        f"样本 {len(labels)} 行 / {len(feature_names)} 特征 / "
        f"正样本 {sum(1 for x in labels if x > 0)}"
    )
    return groups, labels, matrix, feature_names


def _group_sizes(groups: list[str]) -> list[int]:
    """按 groupId 连续段统计每组大小（LambdaRank 的 query group）。"""
    sizes: list[int] = []
    prev = None
    for g in groups:
        if g != prev:
            sizes.append(1)
            prev = g
        else:
            sizes[-1] += 1
    return sizes


def _split_by_group(
    groups: list[str], labels: list[int], matrix: np.ndarray, valid_ratio: float
) -> tuple[list[int], list[int]]:
    """按 group 整体切分训练/验证（同组不跨集，避免同批泄漏）。"""
    order = sorted({g: i for i, g in enumerate(groups)}.values())
    unique_groups = sorted({groups[i] for i in order})
    rng = np.random.default_rng(42)
    rng.shuffle(unique_groups)  # type: ignore[arg-type]
    n_valid = max(1, int(len(unique_groups) * valid_ratio))
    valid_set = set(unique_groups[:n_valid])
    tr = [i for i, g in enumerate(groups) if g not in valid_set]
    va = [i for i, g in enumerate(groups) if g in valid_set]
    return tr, va


def _ndcg_at_k(
    y_true: np.ndarray, y_score: np.ndarray, groups: list[int], k: int
) -> float:
    """按 group 计算 NDCG@K（手写实现，避免额外依赖）。"""

    def _dcg(scores: np.ndarray) -> float:
        return float(np.sum(scores / np.log2(np.arange(2, len(scores) + 2))))

    idx = 0
    total = 0.0
    count = 0
    for size in groups:
        yt = y_true[idx : idx + size]
        ys = y_score[idx : idx + size]
        idx += size
        if size == 0:
            continue
        order = np.argsort(-ys)[:k]
        ideal = np.sort(yt)[::-1][:k]
        denom = _dcg(ideal)
        total += (_dcg(yt[order]) / denom) if denom > 0 else 0.0
        count += 1
    return total / count if count else 0.0


def main() -> None:
    parser = argparse.ArgumentParser(
        description="训练 Feed 精排 LightGBM 模型（2.66.0）"
    )
    parser.add_argument(
        "--samples", type=str, default="data/ml/feed_samples.csv", help="样本 CSV"
    )
    parser.add_argument("--top-k", type=int, default=10, help="NDCG@K 的 K")
    parser.add_argument("--valid-ratio", type=float, default=0.2, help="验证集比例")
    parser.add_argument(
        "--min-positives", type=int, default=50, help="正样本下限（不足则拒绝产出模型）"
    )
    parser.add_argument(
        "--out", type=str, default="", help="模型输出路径（默认取 settings）"
    )
    args = parser.parse_args()

    sample_path = Path(args.samples)
    groups, labels, matrix, feature_names = _load(sample_path)

    positives = sum(1 for x in labels if x > 0)
    if positives < args.min_positives:
        raise SystemExit(
            f"正样本仅 {positives} 条（< {args.min_positives}）：数据量不足以训练，"
            "请先埋点积累或灌数（scripts/seed/）"
        )

    # 按 group 连续排列（LambdaRank 要求同组相邻）
    order = np.argsort(np.asarray(groups, dtype=object), kind="stable")
    groups = [groups[i] for i in order]
    labels = [labels[i] for i in order]
    matrix = matrix[order]

    tr_idx, va_idx = _split_by_group(groups, labels, matrix, args.valid_ratio)
    if not tr_idx or not va_idx:
        raise SystemExit("训练/验证切分后有一侧为空，请增大样本量")

    y_tr = np.asarray([labels[i] for i in tr_idx], dtype=np.float64)
    y_va = np.asarray([labels[i] for i in va_idx], dtype=np.float64)
    x_tr = matrix[tr_idx]
    x_va = matrix[va_idx]

    def _sizes(indices: list[int]) -> list[int]:
        return _group_sizes([groups[i] for i in indices])

    import lightgbm as lgb

    params = {
        "objective": "lambdarank",
        "metric": "ndcg",
        "learning_rate": 0.05,
        "num_leaves": 31,
        "min_data_in_leaf": 20,
        "feature_fraction": 0.9,
        "bagging_fraction": 0.9,
        "bagging_freq": 1,
        "verbosity": -1,
    }
    train_set = lgb.Dataset(x_tr, label=y_tr, group=_sizes(tr_idx))
    valid_set = lgb.Dataset(x_va, label=y_va, group=_sizes(va_idx), reference=train_set)
    booster = lgb.train(
        params,
        train_set,
        num_boost_round=300,
        valid_sets=[valid_set],
        callbacks=[lgb.early_stopping(30, verbose=False)],
    )

    # ---- 离线评估：模型 vs 「按下发位次排」baseline ----
    pred = booster.predict(x_va)
    model_ndcg = _ndcg_at_k(y_va, pred, _sizes(va_idx), args.top_k)
    baseline = np.asarray(
        [-x_va[i][-1] for i in range(len(x_va))], dtype=np.float64
    )  # position 越大排越后
    base_ndcg = _ndcg_at_k(y_va, baseline, _sizes(va_idx), args.top_k)
    from sklearn.metrics import roc_auc_score

    try:
        auc = float(roc_auc_score((y_va > 0).astype(int), pred))
    except ValueError:
        auc = float("nan")

    logger.info(
        f"valid: NDCG@{args.top_k} model={model_ndcg:.4f} "
        f"baseline(position)={base_ndcg:.4f} AUC={auc:.4f}"
    )

    out_path = Path(args.out or settings.ml_ranker_model_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    booster.save_model(str(out_path), num_iteration=booster.best_iteration)
    importance = sorted(
        zip(
            booster.feature_name(),
            booster.feature_importance("gain"),
            strict=False,
        ),
        key=lambda kv: -kv[1],
    )[:8]
    logger.info(f"模型已保存：{out_path}")
    logger.info("特征重要性 top8: " + ", ".join(f"{k}={v:.1f}" for k, v in importance))

    # 训练指标落盘：供 GET /api/v1/community/feed/ml-status 展示（运维判断能不能上）
    metrics_path = Path("data/ml/metrics.json")
    metrics_path.parent.mkdir(parents=True, exist_ok=True)
    metrics_path.write_text(
        json.dumps(
            {
                "trainedAt": datetime.now().isoformat(timespec="seconds"),
                "samples": len(labels),
                "positives": positives,
                "features": len(feature_names),
                "bestIteration": int(booster.best_iteration or 0),
                f"ndcg@{args.top_k}": round(model_ndcg, 6),
                f"ndcg@{args.top_k}_baseline": round(base_ndcg, 6),
                "auc": None if auc != auc else round(auc, 6),
                "topFeatureImportance": {k: round(float(v), 2) for k, v in importance},
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
