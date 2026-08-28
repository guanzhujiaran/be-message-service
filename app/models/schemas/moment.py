"""动态卡片模块请求 / 响应模型（Phase 2）。

**所有 64 位 ID（dynId / mid / repostSrcDynId / topicId）在出参中一律同时提供
int 与 str 两种形态**（`dynId` + `dynIdStr`），避免浏览器 `Number.MAX_SAFE_INTEGER`
精度丢失。服务内部以 int 运算，只在接口边界做转换。

正文 `contentJson` 为富文本节点列表（对齐 B站 Opus/Paragraph 结构），支持的节点类型：
- `WORDS`：纯文本
- `AT`   ：@用户（`bizId`=被@的 mid，`name`=昵称）
- `TOPIC`：话题（`bizId`=话题 id，`name`=话题名）
- `LINK` ：外链（`jumpUrl`=链接，`picMeta.renderAsImage=true` 时前端按图片渲染）

MVP 图片方案：不支持图片上传，仅允许在文字正文里插入**外站图片 URL 链接**节点，
服务端原样存储为 LINK 节点，不做下载 / 存储 / 鉴真。
"""

from typing import Any

from sqlmodel import Field, SQLModel

from app.models.enums import InteractionBizTypeEnum

from app.models.enums import MomentVisibleScopeEnum
from app.models.str_int import StrInt

# ==================== 富文本节点 ====================


from app.models.schemas.base import AutoStrMixin
class MomentContentNode(SQLModel, AutoStrMixin):
    """动态正文中的一个富文本节点。

    节点类型：WORDS / AT / TOPIC / LINK / RESOURCE（2.17.0 新增）。
    `RESOURCE` 用于引用任意业务资源（抽奖卡片 / RPA 操作等），字段：
    `bizType` 资源类型、`bizId` 资源 id、`name` 展示名、`cover` 封面、`jumpUrl` 落地页。
    """

    type: str = Field(description="节点类型：WORDS / AT / TOPIC / LINK / RESOURCE")
    text: str = Field(default="", description="节点文本")
    bizType: InteractionBizTypeEnum | None = Field(default=None, description="资源类型（RESOURCE 节点；InteractionBizTypeEnum 值）")
    bizId: str | None = Field(default=None, description="业务 ID：AT→被@用户mid，TOPIC→话题id，RESOURCE→资源id")
    name: str | None = Field(default=None, description="展示名：AT→昵称，TOPIC→话题名，RESOURCE→资源标题")
    cover: str | None = Field(default=None, description="封面图链接（RESOURCE 节点）")
    jumpUrl: str | None = Field(default=None, description="跳转链接（LINK / RESOURCE 节点）")
    picMeta: dict[str, Any] | None = Field(default=None, description="图片元信息（LINK 且 renderAsImage=true 时）")


class MomentContentParagraph(SQLModel, AutoStrMixin):
    """一个段落（含若干节点）。MVP 简化为单段落多节点，预留多段落扩展。"""

    nodes: list[MomentContentNode] = Field(default_factory=list, description="段落内的富文本节点")


class MomentRepostSrc(SQLModel, AutoStrMixin):
    """转发源引用。"""

    dynId: int = Field(description="转发源动态 ID（int）")


class MomentTopicRef(SQLModel, AutoStrMixin):
    """话题引用。"""

    topicId: int = Field(description="话题 ID（int）")
    topicName: str | None = Field(default=None, description="话题名称")


class MomentLbsRef(SQLModel, AutoStrMixin):
    """LBS 位置引用。"""

    poi: str | None = Field(default=None, description="POI 名称")
    lat: float | None = Field(default=None, description="纬度")
    lng: float | None = Field(default=None, description="经度")


class MomentCreateOption(SQLModel, AutoStrMixin):
    """发布选项。"""

    closeComment: int = Field(default=0, description="是否关闭评论：0=否,1=是")
    # 2.46.0：可见范围——仅 WORD 可设（public/follower/self/charge，缺省 public）；
    # FORWARD 一律强制 public（服务端忽略传入值）
    visibleScope: MomentVisibleScopeEnum | None = Field(
        default=None,
        description="可见范围：0=public,1=follower,2=self,3=charge；缺省 public",
    )


class MomentAttachRef(SQLModel, AutoStrMixin):
    """附加卡资源引用（2.21.0，对标 B 站 `CreateCommonAttachCard { type, biz_id }`）。

    只保存 `bizType` + `bizId`（不冗余存 name/cover/jumpUrl 快照），
    Feed/详情装配时按 `bizType` 经 RPC 实时获取资源详情。
    """

    bizType: InteractionBizTypeEnum = Field(description="资源类型（InteractionBizTypeEnum 值）")
    bizId: str = Field(description="资源 ID（字符串，避免 19 位雪花 ID 精度丢失）")


# ==================== 请求体 ====================


class MomentCreateReq(SQLModel, AutoStrMixin):
    """创建动态请求。

    scene 限定 MVP 支持的两种：WORD（纯文字）/ FORWARD（转发）。
    """

    scene: str = Field(description="动态场景：WORD / FORWARD")
    content: list[MomentContentNode] = Field(description="富文本正文节点列表", min_length=1)
    attach: MomentAttachRef | None = Field(default=None, description="附加卡资源引用（2.21.0，只存 bizType+bizId）")
    repostSrc: MomentRepostSrc | None = Field(default=None, description="转发源（FORWARD 必填）")
    topics: list[MomentTopicRef] | None = Field(
        default=None, description="多话题（2.22.0）：最多 5 个，去重，仅可关联已过审话题"
    )
    topic: MomentTopicRef | None = Field(default=None, description="[兼容]单话题引用（2.22.0 起与 topics 合并去重）")
    lbs: MomentLbsRef | None = Field(default=None, description="LBS 位置")
    option: MomentCreateOption | None = Field(default=None, description="发布选项")


class MomentEditReq(SQLModel, AutoStrMixin):
    """编辑动态请求（rejected / auditing 编辑后自动回 auditing）。"""

    dynId: StrInt = Field(description="动态 ID（int，兼容前端 str 传参）")
    scene: str = Field(description="动态场景：WORD / FORWARD")
    content: list[MomentContentNode] = Field(description="富文本正文节点列表", min_length=1)
    attach: MomentAttachRef | None = Field(default=None, description="附加卡资源引用（2.21.0）")
    topics: list[MomentTopicRef] | None = Field(
        default=None, description="多话题（2.22.0）：最多 5 个，去重，仅可关联已过审话题"
    )
    topic: MomentTopicRef | None = Field(default=None, description="[兼容]单话题引用（2.22.0 起与 topics 合并去重）")
    option: MomentCreateOption | None = Field(default=None, description="发布选项")


class MomentRemoveReq(SQLModel, AutoStrMixin):
    """删除动态请求（软删）。"""

    dynId: StrInt = Field(description="动态 ID（int，兼容前端 str 传参）")


class MomentRepostReq(SQLModel, AutoStrMixin):
    """转发动态请求（FORWARD）。"""

    srcDynId: StrInt = Field(description="源动态 ID（int，兼容前端 str 传参）")
    content: list[MomentContentNode] | None = Field(default=None, description="转发语节点列表（可为空）")


class MomentTopReq(SQLModel, AutoStrMixin):
    """空间置顶 / 取消置顶请求。"""

    dynId: StrInt = Field(description="动态 ID（int，兼容前端 str 传参）")


class MomentCreateCheckReq(SQLModel, AutoStrMixin):
    """发布页预校验请求。"""

    scene: str = Field(description="动态场景：WORD / FORWARD")


class MomentDetailsReq(SQLModel, AutoStrMixin):
    """批量动态详情请求。"""

    dynamicIds: list[StrInt] = Field(description="动态 ID 列表（限 20 条，兼容前端 str 传参）", max_length=20)


class MomentThumbReq(SQLModel, AutoStrMixin):
    """点赞 / 取消点赞请求（2.17.0 泛化支持多业务资源）。

    `bizType` 默认 `dynamic`，此时 `bizId` 等价 `dynId`（二者任传其一）；
    `bizType≠dynamic` 时 `bizId` 必填、`dynId` 忽略。
    """

    bizType: InteractionBizTypeEnum = Field(default=InteractionBizTypeEnum.DYNAMIC, description="资源类型（InteractionBizTypeEnum 值）")
    bizId: StrInt | None = Field(default=None, description="资源 ID（int|str；动态时=动态 ID，兼容前端 str 传参）")
    dynId: StrInt | None = Field(default=None, description="[兼容]动态 ID（int|str，兼容前端 str 传参）")
    up: int = Field(default=1, description="1=点赞, 2=取消点赞")


class MomentReportReq(SQLModel, AutoStrMixin):
    """举报动态请求。"""

    dynId: StrInt = Field(description="被举报动态 ID（int，兼容前端 str 传参）")
    reasonType: int = Field(description="举报原因类型（MomentReportReasonEnum 值）")
    reasonDesc: str | None = Field(default=None, description="补充描述（选填）")


class MomentThumbResp(SQLModel, AutoStrMixin):
    """点赞响应（2.17.0 泛化）。"""

    bizType: InteractionBizTypeEnum = Field(default=InteractionBizTypeEnum.DYNAMIC, description="资源类型（InteractionBizTypeEnum 值）")
    bizId: int = Field(description="资源 ID（int）")
    bizIdStr: str = Field(description="资源 ID（字符串，避免精度丢失）")
    dynId: int | None = Field(default=None, description="[兼容]动态 ID（动态资源时返回）")
    dynIdStr: str | None = Field(default=None, description="[兼容]动态 ID（字符串）")
    isLike: bool = Field(default=False, description="操作后当前用户是否已赞")
    likeCount: int = Field(default=0, description="操作后点赞数")


class MomentDislikeReq(SQLModel, AutoStrMixin):
    """点踩 / 取消点踩请求（2.35.0）。

    对齐点赞：`bizType` 默认 `dynamic`，此时 `bizId` 等价 `dynId`（二者任传其一）。
    当前 MVP 仅支持动态资源（非动态 400）。
    """

    bizType: InteractionBizTypeEnum = Field(default=InteractionBizTypeEnum.DYNAMIC, description="资源类型（当前仅支持 dynamic）")
    bizId: StrInt | None = Field(default=None, description="资源 ID（int|str；动态时=动态 ID，兼容前端 str 传参）")
    dynId: StrInt | None = Field(default=None, description="[兼容]动态 ID（int|str，兼容前端 str 传参）")
    up: int = Field(default=1, description="1=点踩, 2=取消点踩")


class MomentDislikeResp(SQLModel, AutoStrMixin):
    """点踩响应（2.35.0）。"""

    bizType: InteractionBizTypeEnum = Field(default=InteractionBizTypeEnum.DYNAMIC, description="资源类型（InteractionBizTypeEnum 值）")
    bizId: int = Field(description="资源 ID（int）")
    bizIdStr: str = Field(description="资源 ID（字符串，避免精度丢失）")
    dynId: int | None = Field(default=None, description="[兼容]动态 ID")
    dynIdStr: str | None = Field(default=None, description="[兼容]动态 ID（字符串）")
    isDislike: bool = Field(default=False, description="操作后当前用户是否已点踩")
    dislikeCount: int = Field(default=0, description="操作后点踩数")


class MomentShareReq(SQLModel, AutoStrMixin):
    """分享上报请求（2.35.0）。"""

    dynId: StrInt = Field(description="被分享动态 ID（int，兼容前端 str 传参）")


class MomentShareResp(SQLModel, AutoStrMixin):
    """分享上报响应（2.35.0）。"""

    dynId: int = Field(description="动态 ID（int）")
    dynIdStr: str = Field(description="动态 ID（字符串，避免精度丢失）")
    shareCount: int = Field(default=0, description="操作后分享数")


class MomentReportResp(SQLModel, AutoStrMixin):
    """举报响应。"""

    dynId: int = Field(description="被举报动态 ID（int）")
    dynIdStr: str = Field(description="动态 ID（字符串）")
    success: bool = Field(default=True)


# ==================== 响应体 ====================


class MomentBaseResp(SQLModel, AutoStrMixin):
    """动态写操作通用响应。"""

    dynId: int = Field(description="动态 ID（int）")
    dynIdStr: str = Field(description="动态 ID（字符串，避免精度丢失）")
    auditStatus: str = Field(description="审核状态成员名字符串：AUDITING/NORMAL/REJECTED/HIDDEN")
    dynType: str = Field(description="动态类型字符串：WORD/FORWARD")


class MomentCreateResp(MomentBaseResp):
    """创建动态响应。"""


class MomentEditResp(MomentBaseResp):
    """编辑动态响应。"""


class MomentRemoveResp(SQLModel, AutoStrMixin):
    """删除动态响应。"""

    dynId: int = Field(description="动态 ID（int）")
    dynIdStr: str = Field(description="动态 ID（字符串）")
    success: bool = Field(default=True)


class MomentRepostResp(MomentBaseResp):
    """转发动态响应。"""


class MomentTopResp(SQLModel, AutoStrMixin):
    """置顶 / 取消置顶响应。"""

    dynId: int = Field(description="动态 ID（int）")
    dynIdStr: str = Field(description="动态 ID（字符串）")
    isTop: int = Field(description="是否置顶：0=否,1=是")


class MomentCreateCheckResp(SQLModel, AutoStrMixin):
    """发布页预校验响应。"""

    setting: dict = Field(default_factory=dict, description="发布设置（MVP 留空扩展）")
    permission: dict = Field(default_factory=dict, description="权限信息（MVP 留空扩展）")
    allowedScenes: list[str] = Field(default_factory=lambda: ["WORD", "FORWARD"], description="允许的动态场景")


# ==================== Feed / 详情 响应体 ====================


class MomentModule(SQLModel, AutoStrMixin):
    """动态详情 / Feed 卡片中的一个渲染模块（对齐 B站 DynModuleType）。"""

    moduleType: str = Field(
        description="模块类型：author/desc/dynamic/forward/extend/additional/interaction"
    )
    # author 模块
    mid: int | None = Field(default=None, description="发布者 UID")
    uname: str | None = Field(default=None, description="发布者昵称")
    face: str | None = Field(default=None, description="发布者头像")
    ptimeLabelText: str | None = Field(default=None, description="发布时间文案（如 10分钟前）")
    relation: str | None = Field(default=None, description="与当前用户关系：following/stranger")
    # desc / dynamic 模块
    text: str | None = Field(default=None, description="纯文本正文")
    nodes: list[MomentContentNode] | None = Field(default=None, description="富文本节点列表")
    # dynamic（正文卡）模块
    dtype: str | None = Field(default=None, description="正文卡类型：word/forward")
    # forward 嵌套模块
    srcDynId: int | None = Field(default=None, description="转发源动态 ID")
    srcMoment: "MomentFeedItem | None" = Field(
        default=None, description="转发源动态完整卡片（嵌套渲染原动态，含作者/正文/统计）"
    )
    # extend 模块
    topicId: int | None = Field(default=None, description="主话题 ID（= topics[0]，兼容存量单话题客户端）")
    topicName: str | None = Field(default=None, description="主话题名称")
    topics: list[MomentTopicRef] | None = Field(
        default=None, description="多话题（2.22.0）：动态关联的全部话题 [{topicId, topicName}]，按关联顺序"
    )
    # additional（附加卡）模块：只存 bizType+bizId，name/cover/jumpUrl 由装配层 RPC 实时获取
    bizType: InteractionBizTypeEnum | None = Field(default=None, description="附加卡资源类型（InteractionBizTypeEnum 值）")
    bizId: str | None = Field(default=None, description="附加卡资源 ID（字符串）")
    name: str | None = Field(default=None, description="附加卡标题（RPC 实时获取）")
    cover: str | None = Field(default=None, description="附加卡封面（RPC 实时获取）")
    jumpUrl: str | None = Field(default=None, description="附加卡跳转链接（RPC 实时获取）")
    # interaction 模块
    isLike: bool | None = Field(default=None, description="当前用户是否已赞")


class MomentFeedItem(SQLModel, AutoStrMixin):
    """Feed 流 / 详情中的单条动态卡片。"""

    dynId: int = Field(description="动态 ID（int）")
    dynIdStr: str = Field(description="动态 ID（字符串，避免精度丢失）")
    dynType: str = Field(description="动态类型字符串：WORD/FORWARD")
    mid: int = Field(description="发布者 UID")
    auditStatus: str = Field(description="审核状态字符串")
    isTop: int = Field(default=0, description="是否置顶：0=否,1=是")
    pubTime: str | None = Field(default=None, description="发布时间（ISO，仅 normal 有值）")
    createdTime: str | None = Field(default=None, description="创建时间（ISO，用于 auditing/rejected 展示）")
    auditRejectReason: str | None = Field(default=None, description="驳回原因（rejected 状态）")
    ipLocation: str | None = Field(default=None, description="IP 属地（如「浙江 杭州」，服务端 GeoIP 解析）")
    ipIsp: str | None = Field(default=None, description="IP 运营商 ISP")
    modules: list[MomentModule] = Field(default_factory=list, description="渲染模块列表")


class MomentFeedResp(SQLModel, AutoStrMixin):
    """Feed 流响应。

    2.32.0 起综合页 ``sort=recommend`` 为**推荐流模式**（对齐 B 站 rcmd）：
    - 无 page/offset 游标语义，``updateBaseline`` / ``historyOffset`` / ``updateNum`` 置空；
    - ``hasMore`` = 排除客户端 ``last_showlist`` 后候选是否仍有剩余；
    - 请求侧通过 ``last_showlist``（已展示 dynId）驱动服务端去重。

    ``sort=time`` 最新模式仍使用 ``historyOffset`` 游标分页（原语义不变）。
    """

    items: list[MomentFeedItem] = Field(default_factory=list)
    hasMore: bool = Field(default=False, description="是否还有更多（recommend=排除已展示后候选仍有剩余 / time=是否有下一页）")
    updateBaseline: int | None = Field(default=None, description="刷新基线（最新一条 dynId，recommend 模式置空）")
    historyOffset: int | None = Field(default=None, description="历史偏移（最旧一条 dynId，recommend 模式置空）")
    updateNum: int = Field(default=0, description="相对基线的新增条数（recommend 模式恒 0）")


class MomentUpStatResp(SQLModel, AutoStrMixin):
    """空间统计响应（对标 B 站 `/x/space/upstat`）。

    统计某用户对外可见动态的总数与获赞总数。
    """

    mid: int = Field(description="用户 UID")
    dynamic_count: int = Field(default=0, description="对外可见动态总数")
    like_count: int = Field(default=0, description="这些动态被点赞的总数")


class MomentDetailResp(MomentFeedItem):
    """动态详情响应（单条完整卡片）。

    2.41.0：详情/Feed **不返回统计**（外层 ``stat`` 字段与 ``module_stat`` 模块均已移除）
    ——互动状态 / 计数统一由 ``GET /api/v1/community/interaction/status`` 查询。
    """


# MomentModule.srcMoment ↔ MomentFeedItem.modules 存在循环引用，
# 需在全部模型定义完成后重建一次以解析前向引用
MomentFeedItem.model_rebuild()


# ==================== Phase 5：话题 & @ & POI ====================


class MomentTopicInfo(SQLModel, AutoStrMixin):
    """话题广场单条话题信息。"""

    topicId: int = Field(description="话题 ID（int）")
    topicName: str = Field(description="话题名称")
    topicCover: str | None = Field(default=None, description="话题封面图")
    topicDesc: str | None = Field(default=None, description="话题描述")
    jumpUrl: str | None = Field(default=None, description="话题跳转 H5")
    dynCount: int = Field(default=0, description="话题下动态数（仅 normal 且未软删）")
    viewCount: int = Field(default=0, description="话题浏览量")
    isHot: int = Field(default=0, description="是否热门话题")


class MomentTopicSquareResp(SQLModel, AutoStrMixin):
    """话题广场列表响应。"""

    items: list[MomentTopicInfo] = Field(default_factory=list, description="话题列表")
    hasMore: bool = Field(default=False, description="是否还有下一页")


class MomentTopicDetailItem(SQLModel, AutoStrMixin):
    """话题详情（对齐 B 站 `top_details` 结构）。

    - ``topic_item``：话题主体信息（浏览/讨论/收藏/动态/点赞数等）；
    - ``topic_creator``：话题创建者简要信息（uid/face/name）；
    - ``has_create_jurisdiction``：当前用户是否有创建/管理该话题的权限。
    """

    # ---- topic_item ----
    topic_item: dict[str, Any] = Field(
        default_factory=dict,
        description="话题主体信息：id/name/view/discuss/fav/dynamics/like/share/jump_url/back_color/share_pic/description/ctime",
    )
    # ---- topic_creator ----
    topic_creator: dict[str, Any] = Field(
        default_factory=dict, description="话题创建者：uid/face/name"
    )
    operation_content: dict[str, Any] = Field(
        default_factory=dict, description="运营位（暂为空）"
    )
    has_create_jurisdiction: bool = Field(
        default=False, description="当前用户是否有创建/管理权限"
    )
    close_pub_layer_entry: bool = Field(
        default=False, description="是否关闭发布入口"
    )


class MomentTopicDetailResp(SQLModel, AutoStrMixin):
    """话题详情响应（对齐 B 站 `data.top_details`）。"""

    top_details: MomentTopicDetailItem = Field(
        default_factory=MomentTopicDetailItem
    )
    functional_card: dict[str, Any] = Field(default_factory=dict)
    click_area_card: dict[str, Any] = Field(default_factory=dict)


class MomentTopicFeedResp(SQLModel, AutoStrMixin):
    """话题下动态 Feed 响应（复用综合页游标结构）。"""

    topicId: int = Field(description="话题 ID")
    topicName: str = Field(description="话题名称")
    items: list[MomentFeedItem] = Field(default_factory=list)
    hasMore: bool = Field(default=False)
    historyOffset: int | None = Field(default=None)
    updateBaseline: int | None = Field(default=None)
    updateNum: int = Field(default=0)


class MomentAtUserItem(SQLModel, AutoStrMixin):
    """@用户推荐 / 搜索结果项（基础展示信息，来自 pptr）。"""

    mid: int = Field(description="用户 UID")
    uname: str | None = Field(default=None, description="昵称")
    face: str | None = Field(default=None, description="头像")
    remark: str | None = Field(default=None, description="备注 / 关系标签（如「互关」）")


class MomentAtListResp(SQLModel, AutoStrMixin):
    """@用户推荐列表（按分组：关注 / 粉丝）。"""

    following: list[MomentAtUserItem] = Field(default_factory=list, description="我关注的人")
    followers: list[MomentAtUserItem] = Field(default_factory=list, description="我的粉丝")


class MomentAtSearchResp(SQLModel, AutoStrMixin):
    """@用户搜索结果。"""

    items: list[MomentAtUserItem] = Field(default_factory=list)
    hasMore: bool = Field(default=False)


class MomentPoiItem(SQLModel, AutoStrMixin):
    """POI 地点项（本地模式：来自已发 Moment 的 lbsPoi 去重）。"""

    poi: str = Field(description="POI 名称")
    lat: float | None = Field(default=None, description="纬度")
    lng: float | None = Field(default=None, description="经度")
    dynCount: int = Field(default=0, description="使用该 POI 的 Moment 数")


class MomentPoiResp(SQLModel, AutoStrMixin):
    """POI 附近 / 关键词搜索响应（MVP 本地模式，未接外部地图 API）。"""

    items: list[MomentPoiItem] = Field(default_factory=list)
    hasMore: bool = Field(default=False)


# ==================== 2.19.0：话题创建与审核 ====================


class MomentTopicCreateReq(SQLModel, AutoStrMixin):
    """创建话题请求（当前登录用户）。"""

    topicName: str = Field(description="话题名称（1-30 字，全局唯一）")
    topicCover: str | None = Field(default=None, max_length=1024, description="话题封面图（http/https URL）")
    topicDesc: str | None = Field(default=None, max_length=200, description="话题描述（≤200 字）")


class MomentTopicCreateResp(SQLModel, AutoStrMixin):
    """创建话题响应（创建即进入审核，auditStatus='auditing'）。"""

    topicId: int = Field(description="话题 ID")
    topicName: str = Field(description="话题名称")
    auditStatus: str = Field(description="审核状态（auditing）")


class MomentTopicMineItem(SQLModel, AutoStrMixin):
    """「我创建的话题」单条（含审核状态与驳回原因）。"""

    topicId: int = Field(description="话题 ID")
    topicName: str = Field(description="话题名称")
    topicCover: str | None = Field(default=None, description="话题封面图")
    topicDesc: str | None = Field(default=None, description="话题描述")
    auditStatus: str = Field(description="审核状态：auditing/normal/rejected")
    auditRejectReason: str | None = Field(default=None, description="驳回原因（rejected 时有值）")
    pubTime: str | None = Field(default=None, description="审核通过时间（ISO）")
    createdAt: str | None = Field(default=None, description="创建时间（ISO）")


class MomentTopicMineResp(SQLModel, AutoStrMixin):
    """「我创建的话题」列表响应。"""

    items: list[MomentTopicMineItem] = Field(default_factory=list)
    hasMore: bool = Field(default=False)


class MomentTopicAuditItem(SQLModel, AutoStrMixin):
    """管理端话题审核队列中的单条话题（含创建者昵称/头像，pptr 回查）。"""

    topicId: int = Field(description="话题 ID")
    creatorMid: int = Field(description="创建者 UID")
    creatorName: str | None = Field(default=None, description="创建者昵称（pptr 回查）")
    creatorFace: str | None = Field(default=None, description="创建者头像（pptr 回查）")
    topicName: str = Field(description="话题名称")
    topicCover: str | None = Field(default=None, description="话题封面图")
    topicDesc: str | None = Field(default=None, description="话题描述")
    createdAt: str | None = Field(default=None, description="创建时间（ISO）")


class MomentTopicAuditListResp(SQLModel, AutoStrMixin):
    """管理端话题待审核列表响应。"""

    items: list[MomentTopicAuditItem] = Field(default_factory=list)
    total: int = Field(default=0, description="符合条件的总数")
    page_num: int = Field(default=1)
    page_size: int = Field(default=20)


class MomentTopicAuditApproveReq(SQLModel, AutoStrMixin):
    """话题审核通过请求。"""

    topicId: StrInt = Field(description="话题 ID（兼容前端 str 传参）")
    remark: str | None = Field(default=None, description="审核备注（选填）")


class MomentTopicAuditRejectReq(SQLModel, AutoStrMixin):
    """话题审核驳回请求。"""

    topicId: StrInt = Field(description="话题 ID（兼容前端 str 传参）")
    rejectReason: str = Field(description="驳回原因")
    remark: str | None = Field(default=None, description="审核备注（选填）")


# ==================== Phase 6：审核管理 ====================


class MomentAuditItem(SQLModel, AutoStrMixin):
    """管理员审核队列中的单条动态（含作者昵称 / 头像，来自 pptr 回查）。"""

    dynId: int = Field(description="动态 ID（int）")
    dynIdStr: str = Field(description="动态 ID（字符串，避免精度丢失）")
    mid: int = Field(description="发布者 UID")
    authorName: str | None = Field(default=None, description="发布者昵称（pptr 回查）")
    authorFace: str | None = Field(default=None, description="发布者头像（pptr 回查）")
    dynType: str = Field(description="动态类型字符串：WORD/FORWARD")
    contentText: str | None = Field(default=None, description="纯文本正文预览")
    pubTime: str | None = Field(default=None, description="发布时间（ISO，normal 才有）")
    createdTime: str | None = Field(default=None, description="创建时间（ISO）")
    auditStatus: str = Field(description="审核状态字符串")
    isTop: int = Field(default=0, description="是否置顶：0=否,1=是")
    topicId: int | None = Field(default=None, description="关联话题 ID")


class MomentAuditListResp(SQLModel, AutoStrMixin):
    """管理员待审核列表响应。"""

    items: list[MomentAuditItem] = Field(default_factory=list)
    total: int = Field(default=0, description="符合条件的总数")
    page_num: int = Field(default=1)
    page_size: int = Field(default=20)


class MomentAuditActionReq(SQLModel, AutoStrMixin):
    """审核通过请求。"""

    dynId: StrInt = Field(description="动态 ID（int，兼容前端 str 传参）")
    remark: str | None = Field(default=None, description="审核备注（选填）")


class MomentAuditRejectReq(SQLModel, AutoStrMixin):
    """审核驳回请求。"""

    dynId: StrInt = Field(description="动态 ID（int，兼容前端 str 传参）")
    rejectReason: str = Field(description="驳回原因")
    remark: str | None = Field(default=None, description="审核备注（选填）")


class MomentAuditLogItem(SQLModel, AutoStrMixin):
    """单条审核流转记录（管理后台流水）。"""

    pk: int = Field(description="记录主键")
    dynId: int = Field(description="被审核动态 ID")
    operatorMid: int = Field(description="操作人 MID")
    operatorRole: str = Field(description="操作人角色：author/admin")
    fromStatus: str | None = Field(default=None, description="流转前状态")
    toStatus: str = Field(description="流转后状态")
    actionType: str = Field(description="操作类型：create/edit/approve/reject/resubmit/delete")
    rejectReason: str | None = Field(default=None, description="驳回原因（仅 reject）")
    remark: str | None = Field(default=None, description="其他备注")
    createdTime: str | None = Field(default=None, description="操作时间（ISO）")


class MomentAuditLogListResp(SQLModel, AutoStrMixin):
    """审核记录流水响应。"""

    items: list[MomentAuditLogItem] = Field(default_factory=list)
    total: int = Field(default=0)
    page_num: int = Field(default=1)
    page_size: int = Field(default=20)


class MomentAuditDetailResp(SQLModel, AutoStrMixin):
    """单条动态审核详情（含全部状态 + 历史流转）。"""

    item: MomentAuditItem | None = Field(default=None, description="动态当前快照")
    logs: list[MomentAuditLogItem] = Field(default_factory=list, description="审核流转历史")


class MomentAuditTypeStat(SQLModel, AutoStrMixin):
    """单个动态类型的审核状态计数。"""

    dynType: str = Field(description="动态类型：WORD/FORWARD")
    auditing: int = Field(default=0, description="审核中数量")
    normal: int = Field(default=0, description="已过审数量")
    rejected: int = Field(default=0, description="已驳回数量")
    hidden: int = Field(default=0, description="已下架数量")
    total: int = Field(default=0, description="该类型合计")


class MomentAuditStatisticsResp(SQLModel, AutoStrMixin):
    """动态审核总统计：各类型明细 + 全局状态汇总。"""

    byType: list[MomentAuditTypeStat] = Field(
        default_factory=list, description="各动态类型明细（含各状态计数）"
    )
    byStatus: dict[str, int] = Field(
        default_factory=dict, description="全局按审核状态汇总（auditing/normal/rejected/hidden）"
    )
    total: int = Field(default=0, description="动态总数")


# ==================== Phase 8：点赞明细 / 转发列表（P8-T9）====================
class MomentLikerItem(SQLModel, AutoStrMixin):
    """单条点赞记录（用户简要 + 点赞时间）。"""

    mid: int = Field(description="点赞用户 mid")
    uname: str | None = Field(default=None, description="昵称（取不到则为 None）")
    face: str | None = Field(default=None, description="头像 URL")
    like_time: str | None = Field(
        default=None, description="点赞时间（ISO8601；服务端从 TMomentLike.created_at 取）"
    )


class MomentForwardItem(SQLModel, AutoStrMixin):
    """单条转发记录（被转发过来的动态简要）。"""

    dynId: int = Field(description="转发动态 ID")
    mid: int = Field(description="转发者 mid")
    uname: str | None = Field(default=None, description="转发者昵称")
    face: str | None = Field(default=None, description="转发者头像")
    pubTime: str | None = Field(default=None, description="发布时间（ISO8601）")
    text: str | None = Field(default=None, description="转发时的 desc 模块正文（去除富文本）")


class MomentLikerListResp(SQLModel, AutoStrMixin):
    """点赞明细列表响应。"""

    items: list[MomentLikerItem] = Field(default_factory=list, description="点赞用户列表")
    total: int = Field(default=0, description="点赞总数（TInteractionStat.likeCount，2.36.0 起）")
    page_num: int = Field(default=1, description="当前页")
    page_size: int = Field(default=20, description="每页条数")


class MomentForwardListResp(SQLModel, AutoStrMixin):
    """转发列表响应。"""

    items: list[MomentForwardItem] = Field(default_factory=list, description="转发列表")
    total: int = Field(default=0, description="转发总数")
    page_num: int = Field(default=1, description="当前页")
    page_size: int = Field(default=20, description="每页条数")


__all__ = [
    "MomentAtListResp",
    "MomentAtSearchResp",
    "MomentAtUserItem",
    "MomentAttachRef",
    "MomentAuditActionReq",
    "MomentAuditDetailResp",
    "MomentAuditItem",
    "MomentAuditListResp",
    "MomentAuditLogItem",
    "MomentAuditLogListResp",
    "MomentAuditRejectReq",
    "MomentBaseResp",
    "MomentContentNode",
    "MomentContentParagraph",
    "MomentCreateCheckReq",
    "MomentCreateCheckResp",
    "MomentCreateOption",
    "MomentCreateReq",
    "MomentCreateResp",
    "MomentDetailResp",
    "MomentDetailsReq",
    "MomentEditReq",
    "MomentEditResp",
    "MomentFeedItem",
    "MomentFeedResp",
    "MomentLbsRef",
    "MomentModule",
    "MomentPoiItem",
    "MomentPoiResp",
    "MomentRemoveReq",
    "MomentRemoveResp",
    "MomentDislikeReq",
    "MomentDislikeResp",
    "MomentShareReq",
    "MomentShareResp",
    "MomentReportReq",
    "MomentReportResp",
    "MomentRepostReq",
    "MomentRepostResp",
    "MomentRepostSrc",
    "MomentThumbReq",
    "MomentThumbResp",
    "MomentTopReq",
    "MomentTopResp",
    "MomentTopicAuditApproveReq",
    "MomentTopicAuditItem",
    "MomentTopicAuditListResp",
    "MomentTopicAuditRejectReq",
    "MomentTopicCreateReq",
    "MomentTopicCreateResp",
    "MomentTopicDetailItem",
    "MomentTopicDetailResp",
    "MomentTopicFeedResp",
    "MomentTopicInfo",
    "MomentTopicMineItem",
    "MomentTopicMineResp",
    "MomentTopicRef",
    "MomentTopicSquareResp",
]
