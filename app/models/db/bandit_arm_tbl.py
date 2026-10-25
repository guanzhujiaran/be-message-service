"""LinUCB 老虎机臂参数表 ORM（2.66.0，机器学习推荐 P1 探索）。

带上下文的线性老虎机 **LinUCB**（Li et al. 2010）为每个候选资源维护一个「臂」：

    ucb = θᵀx + α · sqrt(xᵀ A⁻¹ x)        θ = A⁻¹ b

- ``A``：``d×d`` 协方差矩阵（按行展开存 JSON 数组，长度 ``dim*dim``）；
- ``b``：``d`` 维奖励向量（JSON 数组）；
- ``x``：上下文特征（内容侧 + 用户侧，见 ``app/services/moment/linucb.py``）。

**更新不在请求路径**：由定时任务回放 ``TFeedEventLog`` 离线重算后整行覆盖
（在线只做 UCB 打分），避免写路径复杂化与事务膨胀。冷启动资源无对应行 →
调用方用单位阵初始化（等价于纯探索）。

与 2.65.0 的无特征 Thompson Sampling 的关系：TS 只看「互动/曝光计数」（无特征、
无法泛化到新内容），LinUCB 用上下文特征泛化，冷启动内容也能被合理探索。
"""

from datetime import datetime

from bili_common.models import InteractionBizTypeEnum
from pydantic import NaiveDatetime
from sqlalchemy import BIGINT, JSON, Index, PrimaryKeyConstraint, UniqueConstraint
from sqlalchemy import Enum as SAEnum
from sqlmodel import Field

from app.models.db.base_tbl import TimestampMixin


class TFeedBanditArm(TimestampMixin, table=True):
    """LinUCB 臂参数：一个 (bizType, bizId) 一行（A 矩阵 + b 向量 + 累计统计）。"""

    __tablename__ = "TFeedBanditArm"
    __table_args__ = (
        PrimaryKeyConstraint("pk", name="TFeedBanditArm_pkey"),
        UniqueConstraint("bizType", "bizId", name="TFeedBanditArm_bizType_bizId_key"),
        Index("idx_bandit_arm_updated", "updated_at"),
        {
            "extend_existing": True,
            "comment": "LinUCB 臂参数：A 矩阵 / b 向量 / 累计曝光与奖励（离线回放更新）",
        },
    )

    pk: int = Field(
        default=None,
        primary_key=True,
        sa_type=BIGINT,
        sa_column_kwargs={"autoincrement": True},
        description="自增主键",
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
    dim: int = Field(
        default=0,
        nullable=False,
        description="上下文特征维度 d（A 为 d×d、b 为 d 维）",
    )
    aMatrix: list = Field(
        default_factory=list,
        sa_type=JSON,
        description="A 矩阵按行展开（长度 dim*dim），用于 xᵀA⁻¹x 置信区间",
    )
    bVector: list = Field(
        default_factory=list,
        sa_type=JSON,
        description="奖励向量 b（长度 dim），θ = A⁻¹b",
    )
    impressionCount: int = Field(
        default=0, nullable=False, description="累计曝光次数（该臂被选择次数）"
    )
    rewardSum: float = Field(
        default=0.0, nullable=False, description="累计奖励（互动分级求和）"
    )
    updatedBy: str = Field(
        default="",
        max_length=32,
        nullable=False,
        description="最后更新来源（train_linucb / manual）",
    )
    lastTrainedAt: NaiveDatetime | None = Field(default=None, description="最后离线训练时间")


__all__ = ["TFeedBanditArm"]
