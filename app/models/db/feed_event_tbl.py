"""Feed 行为事件明细表 ORM（2.66.0，机器学习推荐 P0 埋点）。

一切监督学习的前提是「(曝光, 是否互动)」样本对。``TFeedImpression`` 是**去重点查表**
（一个观众 × 一个场景 × 一个资源一行，累计次数），无法表达「一次曝光」这种流水，
因此这里新增**只追加的事件流水**：

- ``impression``：服务端下发后写（含下发位置 ``position`` 与下发时精排分 ``score``）；
- ``click``：前端上报（卡片点进详情）；
- ``dwell``：前端上报（详情页停留毫秒，``valueMs``）。

样本构建（``scripts/build_feed_samples.py``）把一次 ``impression`` 与曝光后
``ml_label_window_hours`` 内的互动明细 join，得到 graded label
（0 无互动 / 1 点赞 / 2 评论 / 3 转发或收藏），未互动即负样本。

写入策略：只追加、不更新、不删除（除清理任务按 ``ml_event_retention_days`` 归档），
避免与 Feed 读路径的曝光去重表互相干扰。
"""

from bili_common.models import InteractionBizTypeEnum
from sqlalchemy import BIGINT, Index, PrimaryKeyConstraint
from sqlalchemy import Enum as SAEnum
from sqlmodel import Field

from app.models.db.base_tbl import TimestampMixin
from app.models.enums import FeedEventTypeEnum


class TFeedEventLog(TimestampMixin, table=True):
    """Feed 行为事件流水：一次曝光 / 一次点击 / 一次停留一行（模型训练样本源）。"""

    __tablename__ = "TFeedEventLog"
    __table_args__ = (
        PrimaryKeyConstraint("pk", name="TFeedEventLog_pkey"),
        # 按资源统计「曝光-互动」漏斗（离线样本 join 用）
        Index("idx_feed_event_biz_time", "bizType", "bizId", "event", "created_at"),
        # 按观众回放行为序列（LinUCB / 序列特征）
        Index("idx_feed_event_viewer_time", "viewerKey", "created_at"),
        {
            "extend_existing": True,
            "comment": "Feed 行为事件流水：impression/click/dwell（推荐模型训练样本源）",
        },
    )

    pk: int = Field(
        default=None,
        primary_key=True,
        sa_type=BIGINT,
        sa_column_kwargs={"autoincrement": True},
        description="自增主键",
    )
    viewerKey: str = Field(
        max_length=96,
        nullable=False,
        description="观众标识：登录=mid:{mid}，匿名=anon:{uniq_id}（无标识时写空串，不用于去重）",
    )
    bizType: InteractionBizTypeEnum = Field(
        nullable=False,
        sa_type=SAEnum(InteractionBizTypeEnum),
        description="资源类型（IntEnum 落库）：1=dynamic,2=lottery,...",
    )
    bizId: int = Field(
        nullable=False,
        sa_type=BIGINT,
        description="资源 id（动态=dynId）",
    )
    event: FeedEventTypeEnum = Field(
        nullable=False,
        sa_type=SAEnum(FeedEventTypeEnum),
        description="事件类型：impression=下发曝光 / click=点击进详情 / dwell=停留",
    )
    feedScene: str = Field(
        default="",
        max_length=32,
        nullable=False,
        description="Feed 场景：comprehensive=综合页 / topic=话题流",
    )
    position: int | None = Field(
        default=None,
        description="在本次下发列表中的位次（0 起；位置偏差特征用，非下发事件为 NULL）",
    )
    score: float | None = Field(
        default=None,
        description="下发时的精排分（用于离线回归对照 / 位置偏差校正）",
    )
    valueMs: int | None = Field(
        default=None,
        description="dwell 事件的停留毫秒数（click/impression 为 NULL）",
    )


__all__ = ["TFeedEventLog"]
