"""多业务资源互动通用服务（be-message 实现，复用 bili-common 通用逻辑）。

- **通用逻辑**：`InteractionStatService`（非动态资源计数）与 `InteractionResourceValidator`
  （注册式校验器）收口到 bili-common，本模块做 be-message 侧绑定：
  - `BeMessageInteractionStatService`：绑定 `TInteractionStat`；
  - 注册 `dynamic` 资源校验器（校验 `TMoment` 存在且未软删）。
- **计数统一**：2.36.0 起动态与非动态资源统一走 `TInteractionStat`，
  均遵循「明细表幂等 + 计数原子 ±1」范式。
"""

from sqlmodel import col, select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.db import TInteractionStat, TInteractionViewLog, TMoment
from bili_common.models import InteractionBizTypeEnum
from bili_common.services.interaction import (
    DYNAMIC_BIZ_TYPE,
    InteractionStatService,
)


class BeMessageInteractionStatService(InteractionStatService):
    """be-message 的非动态资源计数服务（绑定 TInteractionStat / TInteractionViewLog）。"""

    model = TInteractionStat
    view_log_model = TInteractionViewLog


# 注：动态 / 抽奖的存在性校验已下沉为 DynamicBiz.check_exists() / LotteryBiz.check_exists()
# （继承 InteractionResourceValidator 的注册式逻辑迁移到各资源类，见计划书 §5.11 / C20），
# 此处不再注册校验器；``validate_exists`` / ``_validate_attach`` 经 get_biz(...).check_exists() 调用。


# 动态资源计数读（复用 MomentStatService 语义，此处提供便捷封装避免循环导入）
async def get_dynamic_stat_counts(
    session: AsyncSession, dyn_ids: list[int]
) -> dict[int, dict[str, int]]:
    """批量读取动态资源计数（bizType=dynamic 用 TInteractionStat）。"""
    from app.services.moment.moment_stat import MomentStatService

    rows = await MomentStatService.batch_read_stats(session, dyn_ids)
    return {
        d: {
            "likeCount": int(rows[d].likeCount if d in rows else 0),
            "favoriteCount": int(rows[d].favoriteCount if d in rows else 0),
        }
        for d in dyn_ids
    }


__all__ = [
    "DYNAMIC_BIZ_TYPE",
    "InteractionBizTypeEnum",
    "InteractionResourceValidator",
    "InteractionStatService",
    "BeMessageInteractionStatService",
    "get_dynamic_stat_counts",
]
