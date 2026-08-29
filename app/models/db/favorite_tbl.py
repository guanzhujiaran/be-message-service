"""动态收藏夹 ORM 模型（be-message MySQL 主库 `BiliMessageDB`）。

对齐 `app.models.db` 既有规范（详见 `moment.py` 顶部约束）：
- 表名 `T` 前缀 PascalCase；列名 camelCase；
- `TimestampMixin` 时间戳；`mid` 系字段仅存 BIGINT，不建跨库外键；
- 索引 / 唯一约束在 `__table_args__` 显式声明；
- `folder_id` 复用雪花 ID 生成器（`app.core.sharding.generate_moment_id`），
  对外以字符串传输避免 JS 精度丢失。

收藏夹业务约束（计划书 §4.2 / §4.8）：
- 一个用户多个收藏夹，每用户有且仅有一个**默认收藏夹**（`is_default=1`），
  首次进入收藏时由 `get_or_create` 自动创建，保证新用户无需先建夹即可收藏；
- 收藏夹可自定义封面（`cover_url`，**仅存链接不转存图片**）、名称、描述；
- 收藏明细 `TMomentFavorite` 唯一约束 `(bizType, bizId, folderId)` 保证同一收藏夹
  对同一资源不重复收藏（2.17.0 泛化，支持任意业务资源）；
- 动态资源（`bizType='dynamic'`）与非动态资源（lottery / rpa_*）计数统一走
  `TInteractionStat.favoriteCount`（2.36.0 起），收藏/取消时原子 ±1。
"""

from datetime import datetime

from sqlalchemy import BIGINT, Text, UniqueConstraint, text
from sqlmodel import Field, Index, PrimaryKeyConstraint

from app.models.db.base_tbl import TimestampMixin
from sqlalchemy import Enum as SAEnum
from app.models.enums import InteractionBizTypeEnum


class TFavoriteFolder(TimestampMixin, table=True):
    """动态收藏夹表（一个用户可有多个收藏夹）。"""

    __tablename__ = "TFavoriteFolder"
    __table_args__ = (
        PrimaryKeyConstraint("folder_id", name="TFavoriteFolder_pkey"),
        Index("idx_fav_folder_mid_created", "mid", text('created_at DESC')),
        # 每用户仅一个默认收藏夹（部分唯一索引由迁移创建，MySQL 8 支持）
        {"extend_existing": True, "comment": "动态收藏夹：每用户多夹，一个默认夹"},
    )

    folder_id: int = Field(default=None, primary_key=True, sa_type=BIGINT, sa_column_kwargs={"autoincrement": False})
    mid: int = Field(default=None, nullable=False, sa_type=BIGINT, index=True, description="收藏夹所属用户mid")
    name: str = Field(default="默认收藏夹", max_length=100, description="收藏夹名称")
    description: str | None = Field(default=None, max_length=500, description="收藏夹描述（选填）")
    cover_url: str | None = Field(default=None, max_length=1000, description="封面图片链接（仅存URL，不转存图片）")
    is_default: int = Field(default=0, sa_type=BIGINT, description="是否默认收藏夹：0=否,1=是（每用户一个）")


class TMomentFavorite(TimestampMixin, table=True):
    """收藏明细表（2.17.0 泛化：支持任意业务资源 bizType+bizId）。

    幂等双写：唯一约束 `(bizType, bizId, folderId)` 保证同夹不重复收藏；
    动态资源（bizType='dynamic'）时 `bizId` 与 `dynId` 冗余相同。
    """

    __tablename__ = "TMomentFavorite"
    __table_args__ = (
        PrimaryKeyConstraint("pk", name="TMomentFavorite_pkey"),
        UniqueConstraint("bizType", "bizId", "folderId", name="TMomentFavorite_bizType_bizId_folderId_key"),
        Index("idx_fav_mid_created", "mid", text('created_at DESC')),
        Index("idx_fav_folder_created", "folderId", text('created_at DESC')),
        Index("idx_fav_biz", "bizType", "bizId"),
        {"extend_existing": True, "comment": "收藏明细：唯一约束(bizType,bizId,folderId)防重复收藏"},
    )

    pk: int = Field(default=None, primary_key=True, sa_type=BIGINT, sa_column_kwargs={"autoincrement": True})
    bizType: InteractionBizTypeEnum = Field(
        default=InteractionBizTypeEnum.DYNAMIC,
        sa_type=SAEnum(InteractionBizTypeEnum),
        nullable=False,
        description="资源类型（IntEnum 落库 INT）：1=dynamic,2=lottery,3=rpa_action,4=rpa_workflow,5=rpa_browser",
    )
    bizId: int = Field(default=None, nullable=False, sa_type=BIGINT, description="被收藏的资源id（动态时=dynId）")
    dynId: int | None = Field(default=None, sa_type=BIGINT, index=True, description="冗余兼容列：bizType=dynamic 时与 bizId 相同，非动态为 NULL")
    folderId: int = Field(default=None, nullable=False, sa_type=BIGINT, index=True, description="所属收藏夹id")
    mid: int = Field(default=None, nullable=False, sa_type=BIGINT, index=True, description="收藏用户mid")
    note: str | None = Field(default=None, max_length=200, description="收藏备注（预留）")


class TUserFavoriteSetting(TimestampMixin, table=True):
    """用户主页收藏可见性设置（每用户一行，`get_or_create` 范式）。

    - `showFavorites`：主页是否展示「收藏」tab（默认 1=展示；0=隐藏，
      对访客同样隐藏）。默认显示，由用户自行关闭（计划书 §4.2）。
    """

    __tablename__ = "TUserFavoriteSetting"
    __table_args__ = (
        PrimaryKeyConstraint("mid", name="TUserFavoriteSetting_pkey"),
        {"extend_existing": True, "comment": "用户主页收藏可见性设置：每用户一行"},
    )

    mid: int = Field(default=None, primary_key=True, sa_type=BIGINT, sa_column_kwargs={"autoincrement": False}, description="用户mid")
    showFavorites: int = Field(default=1, sa_type=BIGINT, description="主页是否显示收藏tab：1=显示,0=隐藏")


__all__ = ["TFavoriteFolder", "TMomentFavorite", "TUserFavoriteSetting"]
