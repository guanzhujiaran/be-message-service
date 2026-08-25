"""多业务资源交互 schema（2.17.0 泛化，2.18.0 增加资源详情）。

收藏 / 点赞从「仅动态」泛化为「任意业务资源 bizType+bizId」后的查询态响应；
非动态资源（RPA 等）随详情经 RPC 获取一并返回。
"""

from sqlmodel import Field, SQLModel

from bili_common.rpc.rpa import ResourceDetail


from app.models.schemas.base import AutoStrMixin
class InteractionStatusItem(SQLModel, AutoStrMixin):
    """某资源当前用户交互态（收藏 + 点赞 + 计数）。"""

    bizType: str = Field(description="资源类型")
    bizId: str = Field(description="资源 id（字符串）")
    isLike: bool = Field(default=False, description="当前用户是否已赞")
    isFavorite: bool = Field(default=False, description="当前用户是否已收藏")
    likeCount: int = Field(default=0, description="点赞数")
    favoriteCount: int = Field(default=0, description="收藏数")
    commentCount: int = Field(default=0, description="评论数（dynamic 时=动态统计；非动态资源无评论，恒为 0）")
    repostCount: int = Field(default=0, description="转发数（dynamic 时=动态统计；非动态资源无转发，恒为 0）")
    viewCount: int = Field(default=0, description="浏览数（dynamic 时=TMomentStat.viewCount；非动态=TInteractionStat.viewCount；2.23.0）")
    reportCount: int = Field(default=0, description="被举报次数（累计，COUNT(*)，2.40.0）")
    reportPeopleCount: int = Field(default=0, description="举报人数（去重举报人，COUNT(DISTINCT reportMid)，2.40.0）")
    detail: ResourceDetail | None = Field(default=None, description="资源详情（非动态资源经 RPC 获取，弱依赖可空；2.18.0）")


class InteractionStatusResp(SQLModel, AutoStrMixin):
    """批量交互态查询响应。"""

    items: list[InteractionStatusItem] = Field(default_factory=list, description="各资源交互态")


__all__ = ["InteractionStatusItem", "InteractionStatusResp"]
