"""多业务资源互动通用服务（be-message 实现，复用 bili-common 通用逻辑）。

- **通用逻辑**：`InteractionStatService`（非动态资源计数）与 `InteractionResourceValidator`
  （注册式校验器）收口到 bili-common，本模块做 be-message 侧绑定：
  - `BeMessageInteractionStatService`：绑定 `TInteractionStat`；
  - 注册 `dynamic` 资源校验器（校验 `TMoment` 存在且未软删）。
- **计数双写**：动态资源走 `TMomentStat`，非动态资源走 `TInteractionStat`，
  均遵循「明细表幂等 + 计数原子 ±1」范式。
"""

from sqlmodel import col, select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.db import TInteractionStat, TInteractionViewLog, TMoment
from bili_common.models.interaction import InteractionBizTypeEnum
from bili_common.services.interaction import (
    DYNAMIC_BIZ_TYPE,
    InteractionResourceValidator,
    InteractionStatService,
)


class BeMessageInteractionStatService(InteractionStatService):
    """be-message 的非动态资源计数服务（绑定 TInteractionStat / TInteractionViewLog）。"""

    model = TInteractionStat
    view_log_model = TInteractionViewLog


# 注册 dynamic 资源校验器（校验 TMoment 存在且未软删）
async def _check_dynamic(session: AsyncSession, biz_id: int) -> bool:
    dyn = (
        await session.exec(
            select(TMoment).where(
                col(TMoment.dynId) == biz_id,
                col(TMoment.deletedAt).is_(None),
            )
        )
    ).one_or_none()
    return dyn is not None


# be-message 启动时注册 dynamic 校验（幂等）
InteractionResourceValidator.register(InteractionBizTypeEnum.DYNAMIC, _check_dynamic)


# 注册 lottery 资源校验器（2.20.0）：经抽奖 RPC 校验 lottery_id 是否存在
# 弱依赖：RPC 未连接 / 失败 / 查询不到均视为不存在（点赞/收藏/转发 lottery 时 422 拒绝）
# biz_id 可能为字符串（收藏 FavoriteAddReq.bizId / RESOURCE 节点 bizId 均为 str），统一转 int
async def _check_lottery(session: AsyncSession, biz_id: int) -> bool:
    from app.services.lottery_rpc import get_lottery_rpc_client

    try:
        bid = int(biz_id)
    except (TypeError, ValueError):
        return False
    client = await get_lottery_rpc_client()
    return await client.lottery_exists(bid)


InteractionResourceValidator.register(InteractionBizTypeEnum.LOTTERY, _check_lottery)


# 动态资源计数读（复用 MomentStatService 语义，此处提供便捷封装避免循环导入）
async def get_dynamic_stat_counts(
    session: AsyncSession, dyn_ids: list[int]
) -> dict[int, dict[str, int]]:
    """批量读取动态资源计数（bizType=dynamic 用 TMomentStat）。"""
    from app.services.moment_stat import MomentStatService

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
