"""动态收藏夹 schema。

`folder_id` 是雪花 ID，一律以**字符串**传输（同 rpid：直接以 number 传给浏览器
会触发 JS 精度丢失）。
"""

from pydantic import Field
from sqlmodel import SQLModel

from bili_common.models import InteractionBizTypeEnum


from app.models.schemas.base import auto_str
@auto_str
class FavoriteFolderCreateReq(SQLModel):
    """创建收藏夹。"""

    name: str = Field(min_length=1, max_length=100, description="收藏夹名称")
    description: str | None = Field(default=None, max_length=500, description="收藏夹描述（选填）")
    coverUrl: str | None = Field(default=None, max_length=1000, description="封面图片链接（仅存URL，不转存图片）")


@auto_str
class FavoriteFolderUpdateReq(SQLModel):
    """更新收藏夹（名称/描述/封面，至少一项）。"""

    folderId: str = Field(description="收藏夹id（字符串，雪花ID）")
    name: str | None = Field(default=None, max_length=100, description="收藏夹名称")
    description: str | None = Field(default=None, max_length=500, description="收藏夹描述（传空字符串清除）")
    coverUrl: str | None = Field(default=None, max_length=1000, description="封面图片链接（传空字符串清除）")


@auto_str
class FavoriteFolderDeleteReq(SQLModel):
    """删除收藏夹。"""

    folderId: str = Field(description="收藏夹id（字符串，雪花ID）")


@auto_str
class FavoriteFolderResp(SQLModel):
    """收藏夹信息。"""

    folderId: str = Field(description="收藏夹id（字符串）")
    name: str = Field(description="收藏夹名称")
    description: str | None = Field(default=None, description="收藏夹描述")
    coverUrl: str | None = Field(default=None, description="封面图片链接（仅存审核通过后公开的封面）")
    isDefault: bool = Field(default=False, description="是否默认收藏夹")
    favoriteCount: int = Field(default=0, description="该夹收藏的动态数")
    coverAuditStatus: str | None = Field(default=None, description="封面审核状态：有 pending 审核时为 'pending'，否则为 null（2.28.0）")


@auto_str
class FavoriteAddReq(SQLModel):
    """收藏资源到收藏夹（2.55.0 全面通用化）。

    `bizType` 默认 `dynamic`，此时 `bizId` = `dynId`（**单一字段，无 `dynId` 兼容字段**）；
    `bizType≠dynamic` 时 `bizId` 必填。
    """

    bizType: InteractionBizTypeEnum = Field(default=InteractionBizTypeEnum.DYNAMIC, description="资源类型（InteractionBizTypeEnum 值）")
    bizId: str = Field(description="资源id（字符串；动态时=动态id，雪花ID）")
    folderId: str | None = Field(default=None, description="收藏夹id（字符串，雪花ID；缺省/空则收藏到默认收藏夹）")


@auto_str
class FavoriteRemoveReq(SQLModel):
    """从收藏夹取消收藏（2.55.0 全面通用化）。"""

    bizType: InteractionBizTypeEnum = Field(default=InteractionBizTypeEnum.DYNAMIC, description="资源类型（InteractionBizTypeEnum 值）")
    bizId: str = Field(description="资源id（字符串；动态时=动态id）")
    folderId: str = Field(description="收藏夹id（字符串，雪花ID）")


@auto_str
class FavoriteAddResp(SQLModel):
    """收藏响应（2.55.0 全面通用化）。"""

    bizType: InteractionBizTypeEnum = Field(default=InteractionBizTypeEnum.DYNAMIC, description="资源类型（InteractionBizTypeEnum 值）")
    bizId: str = Field(description="资源id（字符串）")
    folderId: str = Field(description="收藏夹id（字符串）")
    favorited: bool = Field(default=True, description="本次是否新增收藏（False=已在同夹收藏过）")
    favoriteCount: int = Field(default=0, description="该资源最新收藏数（用户去重）")


@auto_str
class FavoriteListReq(SQLModel):
    """某收藏夹下的资源分页。"""

    folderId: str = Field(description="收藏夹id（字符串，雪花ID）")
    bizType: InteractionBizTypeEnum | None = Field(default=None, description="资源类型过滤（缺省返回全部类型；InteractionBizTypeEnum 值）")
    page: int = Field(default=1, ge=1, description="页码")
    pageSize: int = Field(default=20, ge=1, le=50, description="每页数量")


@auto_str
class FavoriteListItem(SQLModel):
    """某收藏夹下的一条资源（2.17.0 新增）。"""

    bizType: InteractionBizTypeEnum = Field(description="资源类型（InteractionBizTypeEnum 值）")
    bizId: str = Field(description="资源id（字符串）")


@auto_str
class FavoriteListResp(SQLModel):
    """某收藏夹下资源列表（2.55.0 全面通用化：bizType+bizId 对）。

    不再提供 `dynIds` 兼容字段——前端按 `items` 自行过滤 `bizType=dynamic` 项。
    """

    folderId: str = Field(description="收藏夹id（字符串）")
    total: int = Field(default=0, description="该夹收藏总数")
    items: list[FavoriteListItem] = Field(default_factory=list, description="当前页资源明细（bizType+bizId 对）")


@auto_str
class FavoriteItemListResp(SQLModel):
    """某收藏夹下资源明细（2.17.0 新增）。"""

    folderId: str = Field(description="收藏夹id（字符串）")
    total: int = Field(default=0, description="该夹收藏总数")
    items: list[FavoriteListItem] = Field(default_factory=list, description="当前页资源明细")


@auto_str
class FavoriteDynFoldersResp(SQLModel):
    """某资源被当前用户收藏在哪些收藏夹（2.55.0 全面通用化）。"""

    bizType: InteractionBizTypeEnum = Field(default=InteractionBizTypeEnum.DYNAMIC, description="资源类型（InteractionBizTypeEnum 值）")
    bizId: str = Field(description="资源id（字符串）")
    folderIds: list[str] = Field(default_factory=list, description="已收藏该资源的收藏夹id列表")


@auto_str
class FavoriteSettingReq(SQLModel):
    """设置主页是否显示收藏。"""

    showFavorites: bool = Field(default=True, description="主页是否显示收藏tab：True=显示,False=隐藏")


@auto_str
class FavoriteSettingResp(SQLModel):
    """主页收藏可见性。"""

    showFavorites: bool = Field(default=True, description="主页是否显示收藏tab")


__all__ = [
    "FavoriteAddReq",
    "FavoriteAddResp",
    "FavoriteDynFoldersResp",
    "FavoriteFolderCreateReq",
    "FavoriteFolderDeleteReq",
    "FavoriteFolderResp",
    "FavoriteFolderUpdateReq",
    "FavoriteItemListResp",
    "FavoriteListItem",
    "FavoriteListReq",
    "FavoriteListResp",
    "FavoriteRemoveReq",
    "FavoriteSettingReq",
    "FavoriteSettingResp",
]
