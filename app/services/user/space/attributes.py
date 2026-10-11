"""用户空间「可见性维度」的属性模型（面向对象，单一真相源）。

每个维度（个人资料 / 登录信息 / 关注列表 / 点赞列表 / 粉丝列表 / 收藏）都是一个
`UserAttribute` 子类实例，自己携带：

- `spec`：key（同时是 DB 列名与响应字段名）、设置页文案、**适用场景**、缺省值、存储位置；
- 行为 `visible()`：本人恒可见，他人按开关；
- 行为 `mask()`：关闭时如何裁剪自己负责的响应字段（仅敏感原文类需要）。

收益（改造前的痛点）：

- `update_flags` / `get_flags` 不再逐字段抄 5 遍 —— 加维度只加一个类 + 注册一行；
- `apply_flags_to_info` 不再是「if not flags.x: info.x = None」的散装判断；
- 门控不再靠 `getattr(flags, "show_follow_list")` 这种**字符串**取开关（拼错只有运行时才炸），
  统一走 `AttributeKey` 枚举 + `attribute_of()`；
- 设置页文案（label / hint）与后端语义、默认值放在一起，前端契约不会漂。

注：`storage` 字段声明开关的存储位置（当前全部为 `TUserSpacePrivacy`）。
"""

from __future__ import annotations

from abc import ABC
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, ClassVar

if TYPE_CHECKING:  # 仅类型标注用，避免与 schemas 形成运行时环
    from app.models.schemas.space import SpaceInfoResp


class AttributeScene(StrEnum):
    """属性生效的场景：决定「关闭开关」时后端该怎么处理。"""

    #: 敏感原文类：关闭后对应字段**不下发**（配合 response_model_exclude_none）
    PROFILE_FIELD = "profile_field"
    #: 列表类：关闭后整类内容不出现（关注列表 / 粉丝列表）
    CONTENT_LIST = "content_list"
    #: 时间线条目类：关闭后该类条目不出现在行为时间线里
    TIMELINE_ENTRY = "timeline_entry"


class AttributeKey(StrEnum):
    """属性 key：**同时**是 DB 列名、响应字段名与请求字段名（三处必须一致）。"""

    PERSONAL_INFO = "show_personal_info"
    TIMELINE = "show_timeline"
    FOLLOW_LIST = "show_follow_list"
    LIKE_LIST = "show_like_list"
    FANS_LIST = "show_fans_list"


class AttributeStorage(StrEnum):
    """开关的存储位置（当前仅 `TUserSpacePrivacy`；保留枚举以便将来接入其它存储）。"""

    SPACE_PRIVACY = "space_privacy"


@dataclass(frozen=True, slots=True)
class AttributeSpec:
    """属性元信息（不可变值对象）。"""

    key: AttributeKey
    label: str
    hint: str
    scenes: tuple[AttributeScene, ...]
    storage: AttributeStorage = AttributeStorage.SPACE_PRIVACY
    #: **缺省值**：无记录时该开关的取值（隐私表默认全关，收藏缺省展示）
    default: bool = False
    #: DB 列名（与 `key` 不同时显式给）
    column: str | None = None

    @property
    def db_column(self) -> str:
        """落库列名：未显式声明时即 key 本身。"""
        return self.column or str(self.key.value)


class UserAttribute(ABC):
    """用户空间的一个可见性维度（子类只需声明 `spec`，需要裁剪时覆写 `mask`）。"""

    spec: ClassVar[AttributeSpec]

    @property
    def key(self) -> AttributeKey:
        return self.spec.key

    @property
    def scenes(self) -> tuple[AttributeScene, ...]:
        return self.spec.scenes

    def default_value(self) -> bool:
        """无记录时的开关取值。"""
        return self.spec.default

    def supports(self, scene: AttributeScene) -> bool:
        """该属性是否作用于指定场景。"""
        return scene in self.spec.scenes

    def visible(self, *, enabled: bool, is_owner: bool) -> bool:
        """`开关值 + 视角` → 是否可见（本人恒可见，这是唯一的规则出口）。"""
        return is_owner or enabled

    def mask(self, info: SpaceInfoResp) -> None:
        """开关关闭时原地裁剪自己负责的响应字段（默认：本属性无字段可裁）。

        只应由 `SpaceVisibility.mask_profile` 在「非本人且开关关闭」时调用；
        子类实现里直接置空字段即可，配合路由的 `response_model_exclude_none=True`
        让字段**不出现在响应里**（前端无需区分「无权限」与「无数据」）。
        """
        return


class PersonalInfoAttribute(UserAttribute):
    """个人资料维度：性别 / 生日 / 邮箱。"""

    spec: ClassVar[AttributeSpec] = AttributeSpec(
        key=AttributeKey.PERSONAL_INFO,
        label="个人资料（性别 / 生日 / 邮箱）",
        hint="关闭后，别人在空间页看不到这些字段",
        scenes=(AttributeScene.PROFILE_FIELD,),
    )

    def mask(self, info: SpaceInfoResp) -> None:
        info.email = None
        info.sex = ""
        info.birthday = 0


class TimelineAttribute(UserAttribute):
    """时间线维度：他人是否能看 TA 的时间线（**整条**，总开关）。"""

    spec: ClassVar[AttributeSpec] = AttributeSpec(
        key=AttributeKey.TIMELINE,
        label="时间线",
        hint="关闭后，别人看不到你的时间线（关注 / 点赞 / 收藏记录）",
        scenes=(AttributeScene.CONTENT_LIST,),
        # 与隐私表其它开关「默认关闭」相反：时间线是主页核心内容，**默认公开**
        default=True,
    )


class FollowListAttribute(UserAttribute):
    """关注列表维度：关系页「关注」列表 + 时间线「关注了谁」条目。"""

    spec: ClassVar[AttributeSpec] = AttributeSpec(
        key=AttributeKey.FOLLOW_LIST,
        label="关注列表",
        hint="关闭后，别人看不到你关注了谁（统计栏「关注」入口也会被拦下）",
        scenes=(AttributeScene.CONTENT_LIST, AttributeScene.TIMELINE_ENTRY),
    )


class LikeListAttribute(UserAttribute):
    """点赞列表维度：时间线「点赞了什么」条目。"""

    spec: ClassVar[AttributeSpec] = AttributeSpec(
        key=AttributeKey.LIKE_LIST,
        label="点赞列表",
        hint="关闭后，别人看不到你点赞了什么",
        scenes=(AttributeScene.TIMELINE_ENTRY,),
    )


class FansListAttribute(UserAttribute):
    """粉丝列表维度：关系页「粉丝」列表 + 主页粉丝模块。"""

    spec: ClassVar[AttributeSpec] = AttributeSpec(
        key=AttributeKey.FANS_LIST,
        label="粉丝列表",
        hint="关闭后，别人看不到你的粉丝列表（主页粉丝模块与统计栏「粉丝」入口一并隐藏）",
        scenes=(AttributeScene.CONTENT_LIST,),
    )


#: 属性注册表（顺序即设置页展示顺序）
SPACE_ATTRIBUTES: tuple[UserAttribute, ...] = (
    PersonalInfoAttribute(),
    TimelineAttribute(),
    FollowListAttribute(),
    LikeListAttribute(),
    FansListAttribute(),
)

_ATTRIBUTE_BY_KEY = {attr.key: attr for attr in SPACE_ATTRIBUTES}


def attribute_of(key: AttributeKey) -> UserAttribute:
    """按 key 取属性对象（不存在即代码上下不一致，直接抛错而不是静默返回 False）。"""
    attr = _ATTRIBUTE_BY_KEY.get(key)
    if attr is None:
        raise KeyError(f"未注册的空间属性：{key}")
    return attr


def attributes_of(scene: AttributeScene) -> tuple[UserAttribute, ...]:
    """取作用于某场景的全部属性。"""
    return tuple(attr for attr in SPACE_ATTRIBUTES if attr.supports(scene))


__all__ = [
    "SPACE_ATTRIBUTES",
    "AttributeKey",
    "AttributeScene",
    "AttributeSpec",
    "AttributeStorage",
    "FansListAttribute",
    "FollowListAttribute",
    "LikeListAttribute",
    "PersonalInfoAttribute",
    "TimelineAttribute",
    "UserAttribute",
    "attribute_of",
    "attributes_of",
]
