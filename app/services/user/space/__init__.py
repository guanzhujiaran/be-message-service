"""用户空间领域模型（面向对象的「用户多维度属性」层）。

分层约定：

| 模块 | 职责 |
| --- | --- |
| `attributes` | 可见性维度的**声明**：每个维度一个类（key / 文案 / 场景 / 缺省值 / 存储位置 + 裁剪行为） |
| `visibility` | 一次请求的可见性**判定上下文**（被查看者 + 访问者 + 开关），唯一判定出口 |
| `relation` | 关注关系值对象（双向状态 → 互关 / 拉黑语义） |
| `membership` | 关注分组归属与计数值对象（替代裸 dict） |
| `stats` | 空间统计数字值对象（聚合多域数字并转 DTO） |
| `briefs` | 批量用户展示信息值对象（区分「未覆盖」与「已注销」） |

上层服务（`space_privacy` / `follow` / `follow_tag` / `space_timeline` / 路由）只依赖
本包暴露的值对象与判定方法，不再各自 `getattr(flags, "...")` 或逐字段裁剪。
"""

from app.services.user.space.attributes import (
    SPACE_ATTRIBUTES,
    AttributeKey,
    AttributeScene,
    AttributeSpec,
    AttributeStorage,
    UserAttribute,
    attribute_of,
    attributes_of,
)
from app.services.user.space.briefs import UserBrief, UserBriefs
from app.services.user.space.membership import TagCount, TagCounts, TagMembership
from app.services.user.space.relation import FollowRelationState
from app.services.user.space.stats import SpaceStatSummary
from app.services.user.space.visibility import SpaceVisibility

__all__ = [
    "SPACE_ATTRIBUTES",
    "AttributeKey",
    "AttributeScene",
    "AttributeSpec",
    "AttributeStorage",
    "FollowRelationState",
    "SpaceStatSummary",
    "SpaceVisibility",
    "TagCount",
    "TagCounts",
    "TagMembership",
    "UserAttribute",
    "UserBrief",
    "UserBriefs",
    "attribute_of",
    "attributes_of",
]
