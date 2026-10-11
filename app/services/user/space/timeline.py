"""空间行为时间线的内部值对象（替代原先到处传的裸 `dict` / `list[dict]`）。

时间线要合并三张表（关注 / 点赞 / 收藏），中间态原先用 `dict` 承载，
调用方靠 `e.get("biz_type")` 猜字段。这里收敛为显式类型：

- `TimelineEntry`：合并排序的**一条记录**（含资源定位与可选收藏夹名）；
- 批量回查结果（dynId → 动态详情、lottery → 展示信息、dynId → 正文摘要）
  统一用通用聚合 `app.services.common.indexed.Indexed[V]` 承载，不再各造一个类。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime

from bili_common.models import InteractionBizTypeEnum


@dataclass(slots=True)
class TimelineEntry:
    """时间线内存记录（三张表合并排序的中间态）。"""

    act_type: str
    acted_at: datetime
    #: 目标用户：follow = 被关注者；like/favorite = 动态作者（非 dynamic 为 None）
    target_mid: int | None = None
    biz_type: InteractionBizTypeEnum | None = None
    biz_id: int | None = None
    #: 动态正文摘要（仅 dynamic 资源有）
    target_text: str | None = None
    #: 动态 id（仅 dynamic 资源有，前端点进详情）
    target_dyn_id: int | None = None
    #: 收藏夹名（仅 favorite 条目有）
    target_folder_name: str | None = None

    @classmethod
    def follow(cls, target_mid: int, acted_at: datetime) -> TimelineEntry:
        """关注条目（无资源卡）。"""
        return cls(act_type="follow", acted_at=acted_at, target_mid=int(target_mid))

    @classmethod
    def resource(
        cls,
        *,
        act_type: str,
        biz_type: InteractionBizTypeEnum,
        biz_id: int,
        author_mid: int | None,
        content: str | None,
        acted_at: datetime,
    ) -> TimelineEntry:
        """点赞 / 收藏条目。

        `author_mid` / `content` 只有资源是 dynamic 时才有值（来自 `TMoment` 回查）；
        非 dynamic 资源没有作者与正文，前端只用外层操作卡 + 内层资源卡展示。
        """
        is_dynamic = biz_type == InteractionBizTypeEnum.DYNAMIC
        return cls(
            act_type=act_type,
            acted_at=acted_at,
            biz_type=biz_type,
            biz_id=int(biz_id),
            # dynamic 但动态已删 / 未过审时也是 None（LEFT JOIN 未命中）
            target_mid=int(author_mid)
            if author_mid is not None and is_dynamic
            else None,
            target_text=content if is_dynamic else None,
            target_dyn_id=int(biz_id) if is_dynamic else None,
        )

    @property
    def is_dynamic(self) -> bool:
        return self.biz_type == InteractionBizTypeEnum.DYNAMIC

    @property
    def is_lottery(self) -> bool:
        return self.biz_type == InteractionBizTypeEnum.LOTTERY

    def with_folder(self, folder_name: str | None) -> TimelineEntry:
        """补上收藏夹名（收藏条目去重后写入），返回新实例（不原地改，避免共享引用被误改）。"""
        return replace(self, target_folder_name=folder_name)


__all__ = ["TimelineEntry"]
