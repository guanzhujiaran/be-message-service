"""事件相关常量与派生集合（单一真相源旁挂的本地配置）。"""
from __future__ import annotations

from bili_common.models.interaction import InteractionBizTypeEnum

# 每张聚合卡片（aggregate 接口）最多展示的触发者头像数
_MAX_ACTORS_PER_GROUP = 3
# msgfeed 单条 users[] 后端返回上限（前端 B 站样式最多展示 2 个，后端多给便于扩展）
_MAX_USERS_PER_ITEM = 4
# 为了在内存里凑齐每组的头像，单次最多回捞的明细条数（防止大分组撑爆内存）
_ACTOR_SCAN_LIMIT = 500
