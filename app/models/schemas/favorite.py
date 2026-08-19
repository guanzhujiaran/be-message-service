"""动态收藏夹 schema。

`folder_id` 是雪花 ID，一律以**字符串**传输（同 rpid：直接以 number 传给浏览器
会触发 JS 精度丢失）。
"""

from pydantic import Field
from sqlmodel import SQLModel


class FavoriteFolderCreateReq(SQLModel):
    """创建收藏夹。"""

    name: str = Field(min_length=1, max_length=100, description="收藏夹名称")
    description: str | None = Field(default=None, max_length=500, description="收藏夹描述（选填）")
    coverUrl: str | None = Field(default=None, max_length=1000, description="封面图片链接（仅存URL，不转存图片）")


class FavoriteFolderUpdateReq(SQLModel):
    """更新收藏夹（名称/描述/封面，至少一项）。"""

    folderId: str = Field(description="收藏夹id（字符串，雪花ID）")
    name: str | None = Field(default=None, max_length=100, description="收藏夹名称")
    description: str | None = Field(default=None, max_length=500, description="收藏夹描述（传空字符串清除）")
    coverUrl: str | None = Field(default=None, max_length=1000, description="封面图片链接（传空字符串清除）")


class FavoriteFolderDeleteReq(SQLModel):
    """删除收藏夹。"""

    folderId: str = Field(description="收藏夹id（字符串，雪花ID）")


class FavoriteFolderResp(SQLModel):
    """收藏夹信息。"""

    folderId: str = Field(description="收藏夹id（字符串）")
    name: str = Field(description="收藏夹名称")
    description: str | None = Field(default=None, description="收藏夹描述")
    coverUrl: str | None = Field(default=None, description="封面图片链接")
    isDefault: bool = Field(default=False, description="是否默认收藏夹")
    favoriteCount: int = Field(default=0, description="该夹收藏的动态数")


class FavoriteAddReq(SQLModel):
    """收藏资源到收藏夹（2.17.0 泛化支持多业务资源）。

    `bizType` 默认 `dynamic`，此时 `bizId` 等价 `dynId`（二者任传其一）；
    `bizType≠dynamic` 时 `bizId` 必填、`dynId` 忽略。
    """

    bizType: str = Field(default="dynamic", description="资源类型：dynamic/lottery/rpa_action/rpa_workflow/rpa_browser（对外文字，DB 存 int）")
    bizId: str | None = Field(default=None, description="资源id（字符串；动态时=动态id）")
    dynId: str | None = Field(default=None, description="[兼容]动态id（字符串，雪花ID；等价 bizId=bizType=dynamic）")
    folderId: str | None = Field(default=None, description="收藏夹id（字符串，雪花ID；缺省/空则收藏到默认收藏夹）")


class FavoriteRemoveReq(SQLModel):
    """从收藏夹取消收藏（2.17.0 泛化）。"""

    bizType: str = Field(default="dynamic", description="资源类型（对外文字，DB 存 int）")
    bizId: str | None = Field(default=None, description="资源id（字符串）")
    dynId: str | None = Field(default=None, description="[兼容]动态id（字符串，雪花ID）")
    folderId: str = Field(description="收藏夹id（字符串，雪花ID）")


class FavoriteAddResp(SQLModel):
    """收藏响应（2.17.0 泛化）。"""

    bizType: str = Field(default="dynamic", description="资源类型")
    bizId: str = Field(description="资源id（字符串）")
    dynId: str | None = Field(default=None, description="[兼容]动态id（动态资源时返回）")
    folderId: str = Field(description="收藏夹id（字符串）")
    favorited: bool = Field(default=True, description="本次是否新增收藏（False=已在同夹收藏过）")
    favoriteCount: int = Field(default=0, description="该资源最新收藏数（用户去重）")


class FavoriteListReq(SQLModel):
    """某收藏夹下的资源分页。"""

    folderId: str = Field(description="收藏夹id（字符串，雪花ID）")
    bizType: str | None = Field(default=None, description="资源类型过滤（缺省返回全部类型；对外文字，DB 存 int）")
    page: int = Field(default=1, ge=1, description="页码")
    pageSize: int = Field(default=20, ge=1, le=50, description="每页数量")


class FavoriteListItem(SQLModel):
    """某收藏夹下的一条资源（2.17.0 新增）。"""

    bizType: str = Field(description="资源类型")
    bizId: str = Field(description="资源id（字符串）")


class FavoriteListResp(SQLModel):
    """某收藏夹下资源列表（2.17.0 泛化：bizType+bizId 对）。"""

    folderId: str = Field(description="收藏夹id（字符串）")
    total: int = Field(default=0, description="该夹收藏总数")
    dynIds: list[str] = Field(default_factory=list, description="[兼容]当前页动态id列表（仅 bizType=dynamic 时有值）")
    items: list[FavoriteListItem] = Field(default_factory=list, description="当前页资源明细（bizType+bizId 对）")


class FavoriteItemListResp(SQLModel):
    """某收藏夹下资源明细（2.17.0 新增）。"""

    folderId: str = Field(description="收藏夹id（字符串）")
    total: int = Field(default=0, description="该夹收藏总数")
    items: list[FavoriteListItem] = Field(default_factory=list, description="当前页资源明细")


class FavoriteDynFoldersResp(SQLModel):
    """某资源被当前用户收藏在哪些收藏夹（2.17.0 泛化）。"""

    bizType: str = Field(default="dynamic", description="资源类型")
    bizId: str = Field(description="资源id（字符串）")
    dynId: str | None = Field(default=None, description="[兼容]动态id（动态资源时返回）")
    folderIds: list[str] = Field(default_factory=list, description="已收藏该资源的收藏夹id列表")


class FavoriteSettingReq(SQLModel):
    """设置主页是否显示收藏。"""

    showFavorites: bool = Field(default=True, description="主页是否显示收藏tab：True=显示,False=隐藏")


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
