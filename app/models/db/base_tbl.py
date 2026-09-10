"""数据库表模型公共基类与列类型工具。"""

from sqlalchemy import BIGINT
from sqlmodel import Field, SQLModel
from sqlalchemy import Enum as SAEnum

from bili_common.models.db import BaseTimestamp
from bili_common.models.interaction import InteractionBizTypeEnum

# 统一创建/更新时间基类：见 bili_common.models.db.BaseTimestamp
# （created_at index=True + updated_at onupdate），避免各项目重复定义。
TimestampMixin = BaseTimestamp


class ResourceBase(SQLModel):
    """通用资源型表抽象基类（`table=False`，业务子表 `table=True` 继承并建表）。

    承载「任意业务资源上发生的操作 / 记录」共用的最小骨架：主键 + 资源定位
    + 操作者 + 时间戳；与 `TimestampMixin` 一样按 mixin 写法被各子表 `table=True`
    继承。各子表追加自己的业务专属字段（如下踩 / 审核 action / 举报 reasonType）。

    字段：
    - `pk`：BIGINT 自增主键；
    - `bizType`：业务资源类型（`InteractionBizTypeEnum` 的 int 值，与
      `bili-common` 完全对齐；落 INT 而非原生 ENUM，落库字符串比较问题）。
    - `bizId`：业务资源 id；dynamic 时 = `dynId`，lottery/rpa_*/comment/user
      等分别为各自资源 id；
    - `mid`：发起该操作 / 记录的用户 UID——点赞者、点踩者、审核操作人、
      举报人等"对该资源施加动作的 mid"，与子表业务语义一致。
    """

    pk: int | None = Field(
        default=None,
        primary_key=True,
        sa_type=BIGINT,
        sa_column_kwargs={"autoincrement": True},
    )
    bizType: InteractionBizTypeEnum = Field(
        default=None,
        sa_type=SAEnum(InteractionBizTypeEnum),
        nullable=False,
        description="业务资源类型（InteractionBizTypeEnum 值）：1=dynamic,2=lottery,..."
        "rpa_action/rpa_workflow/rpa_browser/rpa_plugin/comment/user 等",
    )
    bizId: int = Field(
        default=None,
        sa_type=BIGINT,
        nullable=False,
        description="业务资源 id：dynamic→dynId,comment→rpid,user→mid,lottery/rpa_*→各自资源 id",
    )
    mid: int = Field(
        default=None,
        sa_type=BIGINT,
        nullable=False,
        description="发起该操作的用户 UID（操作者 / 触发者 / 审核员 / 举报人等统一命名）",
    )


__all__ = ["TimestampMixin", "ResourceBase"]
