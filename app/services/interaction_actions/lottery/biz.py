"""抽奖卡片（LOTTERY）资源类（2.48.0）。

行为与通用资源一致，唯一差异：2.40.0 起 **lottery（crawler 资源）不允许下架**——
仅记录举报 + 转审核，Feed 层 / RPC 处置均不执行，故覆盖 `hide()` 跳过。
"""

from loguru import logger

from bili_common.models import InteractionBizTypeEnum
from app.services.interaction_actions.common.generic_biz import GenericResourceBiz

__all__ = ["LotteryBiz"]


class LotteryBiz(GenericResourceBiz):
    """抽奖卡片资源。"""

    _biz_type = InteractionBizTypeEnum.LOTTERY

    async def check_exists(self) -> bool:
        """经抽奖 RPC 校验 lottery_id 是否存在（弱依赖：失败 / 未连接视为不存在）。"""
        from app.services.infrastructure.lottery_rpc import get_lottery_rpc_client

        try:
            bid = int(self.biz_id)
        except (TypeError, ValueError):
            return False
        try:
            client = await get_lottery_rpc_client()
            return await client.lottery_exists(bid)
        except Exception:  # noqa: BLE001
            return False

    @classmethod
    async def batch_get_resources(cls, session, biz_ids, *, actor_mid=None, rpid_map=None):
        """抽奖批量回捞：并行 ``get_resource_detail`` RPC，按 lotteryId 装配快照。"""
        from asyncio import gather

        from app.models.schemas.interaction import InteractionResource
        from app.services.infrastructure.rpa_rpc import rpa_rpc_client
        from app.utils.route_target import jump_target_for
        from bili_common.models import InteractionBizTypeEnum

        rpid_map = rpid_map or {}
        tasks = {
            bid: rpa_rpc_client.get_resource_detail(
                InteractionBizTypeEnum.LOTTERY.to_text(), bid
            )
            for bid in biz_ids
            if isinstance(bid, int)
        }
        details = dict(zip(tasks.keys(), await gather(*tasks.values()))) if tasks else {}
        out: dict[int, InteractionResource] = {}
        for bid in biz_ids:
            detail = details.get(bid)
            exists = detail is not None
            name = getattr(detail, "name", None) if detail else None
            cover = getattr(detail, "cover", None) if detail else None
            am = getattr(detail.detail, "authorMid", None) if (detail and detail.detail) else None
            out[bid] = InteractionResource(
                bizType=InteractionBizTypeEnum.LOTTERY,
                bizId=bid,
                authorMid=int(am) if am is not None else None,
                exists=exists,
                interactable=exists,
                title=str(name) if name else None,
                cover=str(cover) if cover else None,
                jumpTarget=jump_target_for(
                    InteractionBizTypeEnum.LOTTERY, bid, rpid_map.get(bid)
                ),
            )
        return out

    async def hide(self, *, operator_mid: int = 0, **kwargs) -> None:
        """lottery 不允许下架：跳过全部处置（Feed 层 / RPC 均不执行）。"""
        logger.info(f"lottery 不允许下架，跳过处置: bizId={self.biz_id}")
