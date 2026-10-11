"""推荐机器学习状态模型（2.66.0）。

运维/管理端查看「三件套」当前状态用：开关（运行时配置 ``feed_ml``）、模型文件是否就绪、
上次离线训练指标（``data/ml/metrics.json``，由 ``scripts/train_feed_ranker.py`` 写出）。
"""

from datetime import datetime

from bili_common.core.time_utils import OptionalUnixTime
from sqlmodel import Field, SQLModel

from app.models.schemas.sys_config import FeedMLConfig


class MLModelStatus(SQLModel):
    """单个模型文件的状态。"""

    ready: bool = Field(
        default=False, description="模型/索引文件是否存在（在线可加载）"
    )
    path: str = Field(default="", description="文件路径")
    sizeBytes: int = Field(default=0, description="文件大小（字节），0=不存在")
    trainedAt: OptionalUnixTime = Field(
        default=None, description="文件最后修改时间（视为训练产出时间，秒级时间戳）"
    )
    metrics: dict | None = Field(
        default=None,
        description="上次离线训练指标（样本数 / NDCG / AUC 等，训练脚本产出）",
    )


class FeedMLStatusResp(SQLModel):
    """推荐机器学习状态响应（只读）。"""

    config: FeedMLConfig = Field(
        description="当前生效的运行时配置（feed_ml 配置项，未配置时为 settings 默认）"
    )
    ranker: MLModelStatus = Field(description="LightGBM 精排模型")
    als: MLModelStatus = Field(description="ALS 协同召回索引")
    linucbArms: int = Field(default=0, description="LinUCB 臂参数行数（0=未回放训练）")
    eventSamples: int = Field(
        default=0, description="曝光事件条数（样本构建的原料，< 千条时模型不可信）"
    )


__all__ = ["FeedMLStatusResp", "MLModelStatus"]
