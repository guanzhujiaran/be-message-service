"""多业务资源交互 schema（2.17.0 泛化，2.18.0 增加资源详情）。

收藏 / 点赞从「仅动态」泛化为「任意业务资源 bizType+bizId」后的查询态响应；
非动态资源（RPA 等）随详情经 RPC 获取一并返回。
"""

from sqlmodel import Field, SQLModel

from bili_common.rpc.rpa import ResourceDetail


from app.models.enums import InteractionBizTypeEnum
from app.models.schemas.base import AutoStrMixin
class InteractionResource(SQLModel):
    """互动目标资源的**统一表示**（2.47.0；非 DB 表）。

    各互动操作子类的 ``get_resource()`` 统一返回本模型，把具体资源
    （`TMoment` / RPC 详情等）折叠成统一字段，供基类统一做
    存在性（``exists``）/ 可互动性（``interactable``）/ 作者关系
    （``author_mid``，关注 / 黑名单校验）判断，以及 ``after_execute`` hook
    取作者 / 内容。
    """

    bizType: InteractionBizTypeEnum = Field(description="资源类型")
    bizId: int = Field(description="资源 id（动态时 = dynId）")
    authorMid: int | None = Field(default=None, description="资源作者 mid（未知/非动态资源可为空）")
    ownerMid: int | None = Field(default=None, description="资源所有者 mid（DAC `OWNER_ONLY` 校验依据；未知时经 RPC/子类覆盖判断）")
    exists: bool = Field(default=True, description="资源是否存在")
    interactable: bool = Field(default=True, description="是否可互动（如动态需 normal 未软删；收藏语义允许 auditing）")
    title: str | None = Field(default=None, description="附加展示字段（可选，通知等使用）")
    content: str | None = Field(default=None, description="内容摘要（可选，通知等使用）")


class InteractionStatusItem(SQLModel, AutoStrMixin):
    """某资源当前用户交互态（收藏 + 点赞 + 计数）。"""

    bizType: InteractionBizTypeEnum = Field(description="资源类型（InteractionBizTypeEnum 值）")
    bizId: str = Field(description="资源 id（字符串）")
    isLike: bool = Field(default=False, description="当前用户是否已赞")
    isFavorite: bool = Field(default=False, description="当前用户是否已收藏")
    likeCount: int = Field(default=0, description="点赞数")
    favoriteCount: int = Field(default=0, description="收藏数")
    commentCount: int = Field(default=0, description="评论数（dynamic 时=动态统计；非动态资源无评论，恒为 0）")
    repostCount: int = Field(default=0, description="转发数（dynamic 时=动态统计；非动态资源无转发，恒为 0）")
    viewCount: int = Field(default=0, description="浏览数（dynamic 与非动态统一=TInteractionStat.viewCount）")
    reportCount: int = Field(default=0, description="被举报次数（累计，COUNT(*)，2.40.0）")
    reportPeopleCount: int = Field(default=0, description="举报人数（去重举报人，COUNT(DISTINCT reportMid)，2.40.0）")
    detail: ResourceDetail | None = Field(default=None, description="资源详情（非动态资源经 RPC 获取，弱依赖可空；2.18.0）")


class InteractionStatusResp(SQLModel, AutoStrMixin):
    """批量交互态查询响应。"""

    items: list[InteractionStatusItem] = Field(default_factory=list, description="各资源交互态")


__all__ = ["InteractionResource", "InteractionStatusItem", "InteractionStatusResp"]
