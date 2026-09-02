"""通用收藏 ORM 模型（be-message MySQL 主库 `BiliMessageDB`，2.55.0 起通用化）。

对齐 `app.models.db` 既有规范（详见 `moment.py` 顶部约束）：
- 表名 `T` 前缀 PascalCase；列名 camelCase；
- `TimestampMixin` 时间戳；`mid` 系字段仅存 BIGINT，不建跨库外键；
- 索引 / 唯一约束在 `__table_args__` 显式声明；
- `folder_id` 复用雪花 ID 生成器（`app.core.sharding.generate_moment_id`），
  对外以字符串传输避免 JS 精度丢失。

收藏夹业务约束（计划书 §4.2 / §4.8 / §3.1.1）：
- 一个用户多个收藏夹，每用户有且仅有一个**默认收藏夹**（`is_default=1`），
  首次进入收藏时由 `get_or_create` 自动创建，保证新用户无需先建夹即可收藏；
- 收藏夹可自定义封面（`cover_url`，**仅存链接不转存图片**）、名称、描述；
- **收藏明细 `TResourceFavorite` 继承 `ResourceBase`**（2.55.0 收口：去 `dynId`
  冗余列与对 TMoment 的 FK；类名/表名从 `TMomentFavorite → TResourceFavorite`，
  与 `TResourceLike` / `TResourceDislike` / `TResourceAuditLog` 同前缀），
  唯一约束 `(bizType, bizId, folderId)` 保证同一收藏夹对同一资源不重复收藏
  （2.17.0 泛化，支持任意业务资源）；
- 动态资源（`bizType='dynamic'`）与非动态资源（lottery / rpa_*）计数统一走
  `TInteractionStat.favoriteCount`（2.36.0 起），收藏/取消时原子 ±1。
"""

from datetime import datetime

from sqlalchemy import BIGINT, Text, UniqueConstraint, text
from sqlmodel import Field, Index, PrimaryKeyConstraint

from app.models.db.base_tbl import ResourceBase, TimestampMixin
from sqlalchemy import Enum as SAEnum
from bili_common.models import InteractionBizTypeEnum


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


class TResourceFavorite(ResourceBase, TimestampMixin, table=True):
    """通用资源收藏明细表（2.55.0 改名 `TMomentFavorite → TResourceFavorite`）。

    **继承 `ResourceBase`**（`pk+bizType+bizId+mid+created_at/updated_at`），
    唯一约束 `(bizType, bizId, folderId)` 保证同一收藏夹对同一资源不重复收藏；
    动态资源（bizType=DYNAMIC）时 `bizId` 与 `dynId` 同义（不再冗余 `dynId` 列，
    2.55.0 删除以彻底去掉对 `TMoment` 的 FK 依赖，使任何 bizType 资源都能走同一套
    明细 + 清理路径）。

    类名/表名从 `TMomentFavorite` 改为 `TResourceFavorite`（与 `TResourceLike` /
    `TResourceDislike` / `TResourceAuditLog` 同前缀；语义上 Favorite 适用于任意
    通用资源）。代码层类名统一，DB 层由用户手动更新（计划书 §3.1.1 已附 DDL）。
    """

    __tablename__ = "TResourceFavorite"
    __table_args__ = (
        PrimaryKeyConstraint("pk", name="TResourceFavorite_pkey"),
        UniqueConstraint("bizType", "bizId", "folderId", name="TResourceFavorite_bizType_bizId_folderId_key"),
        Index("idx_resource_favorite_mid_created", "mid", text('created_at DESC')),
        Index("idx_resource_favorite_folder_created", "folderId", text('created_at DESC')),
        Index("idx_resource_favorite_biz", "bizType", "bizId"),
        {"extend_existing": True, "comment": "通用资源收藏明细：同夹内唯一约束(bizType,bizId,folderId)防重复收藏；继承 ResourceBase；原 TMomentFavorite"},
    )

    folderId: int = Field(default=None, nullable=False, sa_type=BIGINT, index=True, description="所属收藏夹id")
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


__all__ = ["TFavoriteFolder", "TResourceFavorite", "TUserFavoriteSetting"]
