"""第三方抽奖动态（OTHERS_LOT_DYN）资源类（2.61.0）。

与 lottery 的差异（**两个独立命名空间，不可混用**）：
- lottery 的 bizId 是 `dyndetail.lotdata.lottery_id`，经 `check_lottery_exist` 校验；
- 本资源的 bizId 是 `biliopusdb.t_lotdyninfo.dynId`，经 `check_others_lot_dyn_exist` 校验。

第三方抽奖动态是**站外 B 站动态**，本站不能下架它，故 `hide()` 与 lottery 一样 no-op。
"""

from loguru import logger

from bili_common.models import InteractionBizTypeEnum
from app.models.schemas.interaction import InteractionResource
from app.services.interaction_actions.common.generic_biz import GenericResourceBiz

__all__ = ["OthersLotDynBiz"]


class OthersLotDynBiz(GenericResourceBiz):
    """第三方抽奖动态资源。"""

    _biz_type = InteractionBizTypeEnum.OTHERS_LOT_DYN

    async def check_exists(self) -> bool:
        """经抽奖 RPC 校验 dynId 是否存在（弱依赖：失败 / 未连接视为不存在）。"""
        from app.services.infrastructure.lottery_rpc import get_lottery_rpc_client

        try:
            did = int(self.biz_id)
        except (TypeError, ValueError):
            return False
        try:
            client = await get_lottery_rpc_client()
            return await client.others_lot_dyn_exists(did)
        except Exception:  # noqa: BLE001
            return False

    async def check_exists_state(self) -> bool | None:
        """三态存在性（2.63.0）：RPC 不可用返回 ``None``，与「明确不存在」区分开。

        供浏览计数消费端使用：明确不存在 → 丢弃脏消息；不可用 → 按弱依赖继续计数。
        """
        from app.services.infrastructure.lottery_rpc import get_lottery_rpc_client

        try:
            did = int(self.biz_id)
        except (TypeError, ValueError):
            return False
        try:
            client = await get_lottery_rpc_client()
            existing = await client.get_existing_others_lot_dyn_ids([did])
        except Exception:  # noqa: BLE001
            return None
        if existing is None:
            return None
        return did in existing

    async def _load_meta(self) -> tuple[str | None, str | None]:
        """经 RPC 取卡片标题（作者名 + 「的抽奖动态」）；第三方动态无封面。"""
        detail = (await self._details([int(self.biz_id)])).get(int(self.biz_id))
        if not detail:
            return None, None
        name = detail.get("name")
        cover = detail.get("cover")
        return (str(name) if name else None), (str(cover) if cover else None)

    async def _load_author_mid(self) -> int | None:
        """动态作者 mid（t_lotdyninfo.up_uid）。"""
        detail = (await self._details([int(self.biz_id)])).get(int(self.biz_id))
        am = detail.get("authorMid") if detail else None
        return int(am) if am is not None else None

    @staticmethod
    async def _details(dyn_ids: list[int]) -> dict[int, dict[str, object | None]]:
        """批量取详情（内部复用，弱依赖：失败返回空 dict）。"""
        from app.services.infrastructure.lottery_rpc import get_lottery_rpc_client

        try:
            client = await get_lottery_rpc_client()
            return await client.get_others_lot_dyn_details(dyn_ids)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[OthersLotDynBiz] 详情回捞失败（降级）: {dyn_ids} {e}")
            return {}

    @classmethod
    async def batch_get_resources(cls, session, biz_ids, *, actor_mid=None, rpid_map=None):
        """批量回捞：一次 RPC 按 dynId 装配快照（对齐 LotteryBiz，防 N+1）。"""
        from app.utils.route_target import jump_target_for

        rpid_map = rpid_map or {}
        ids = [int(b) for b in biz_ids]
        details = await cls._details(ids)
        out: dict[int, InteractionResource] = {}
        for bid in ids:
            detail = details.get(bid)
            exists = detail is not None
            am = detail.get("authorMid") if detail else None
            name = detail.get("name") if detail else None
            cover = detail.get("cover") if detail else None
            out[bid] = InteractionResource(
                bizType=InteractionBizTypeEnum.OTHERS_LOT_DYN,
                bizId=bid,
                authorMid=int(am) if am is not None else None,
                exists=exists,
                interactable=exists,
                title=str(name) if name else None,
                cover=str(cover) if cover else None,
                jumpTarget=jump_target_for(
                    InteractionBizTypeEnum.OTHERS_LOT_DYN, bid, rpid_map.get(bid)
                ),
            )
        return out

    async def resolve_accused(self) -> int:
        """取被举报动态的作者 mid（t_lotdyninfo.up_uid；弱依赖失败返回 0 = 未知作者）。

        覆盖基类（基类走 RPA RPC，对第三方抽奖动态永远取不到）。
        """
        mid = await self._load_author_mid()
        return int(mid) if mid else 0

    async def hide(self, *, operator_mid: int = 0, **kwargs) -> None:
        """第三方抽奖动态是站外 B 站内容，本站不下架：跳过全部处置。"""
        logger.info(f"others_lot_dyn 不允许下架，跳过处置: bizId={self.biz_id}")
