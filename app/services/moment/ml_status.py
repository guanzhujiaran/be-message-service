"""推荐机器学习状态查询（2.66.0）：「开关开了有没有模型在跑」。

配合运行时配置（``feed_ml``）使用：配置决定**是否启用**，本服务回答**是否就绪**
（模型文件在不在、上次离线指标如何、样本够不够）。
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from loguru import logger
from sqlmodel import col, func, select

from app.core.config import settings
from app.core.database import SessionDep
from app.models.db import TFeedBanditArm, TFeedEventLog
from app.models.schemas.ml_status import FeedMLStatusResp, MLModelStatus
from app.models.schemas.sys_config import FeedMLConfig
from app.services.common.runtime_config import get_feed_ml_config
from app.services.moment.als_recall import als_recall_service

_METRICS_PATH = Path("data/ml/metrics.json")


def _model_status(path_str: str) -> MLModelStatus:
    """模型文件状态 + 离线指标（``metrics.json`` 不存在则 metrics=None）。"""
    path = Path(path_str)
    try:
        st = path.stat()
        ready, size, mtime = True, int(st.st_size), datetime.fromtimestamp(st.st_mtime)
    except OSError:
        ready, size, mtime = False, 0, None
    metrics: dict | None = None
    if _METRICS_PATH.exists():
        try:
            metrics = json.loads(_METRICS_PATH.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:  # pragma: no cover - 脏文件防御
            logger.warning(f"[ml_status] 读取训练指标失败: {e}")
    return MLModelStatus(
        ready=ready, path=path_str, sizeBytes=size, trainedAt=mtime, metrics=metrics
    )


async def get_status(session: SessionDep) -> FeedMLStatusResp:
    """汇总三件套状态：生效配置 + 模型文件 + 臂参数行数 + 样本量。"""
    config: FeedMLConfig = await get_feed_ml_config(session)
    arms = (
        await session.exec(
            select(func.count(TFeedBanditArm.pk)).where(
                col(TFeedBanditArm.bizType) == "DYNAMIC"
            )
        )
    ).one()
    events = (
        await session.exec(
            select(func.count(TFeedEventLog.pk)).where(
                col(TFeedEventLog.event) == "IMPRESSION"
            )
        )
    ).one()
    return FeedMLStatusResp(
        config=config,
        ranker=_model_status(settings.ml_ranker_model_path),
        # ALS 索引若已加载过，直接用内存索引规模（比 stat 更贴近「在线真正在用」）
        als=als_recall_service.status(settings.ml_als_model_path),
        linucbArms=int(arms or 0),
        eventSamples=int(events or 0),
    )


__all__ = ["get_status"]
