"""抽奖资源 RPC 客户端（be-message 侧，2.20.0 新增）。

be-message 作为 RPC 客户端，经 `FastapiApp.rpc.check_lottery_exist` 同步调用
be-bilibili-crawler 的 lottery RPC 服务端，校验指定 lottery_id 是否存在，
供点赞 / 收藏 / 转发 lottery 资源时做资源存在性校验。

2.61.0 追加第三方抽奖动态（`others_lot_dyn`，bizId = `t_lotdyninfo.dynId`）校验：
经 `FastapiApp.rpc.check_others_lot_dyn_exist` 校验，与 lottery 的 `lotdata.lottery_id`
**是两个独立命名空间**（第三方动态没有 lottery_id，混用必然判不存在）。

契约（方法名 / 参数 / 响应）见 `bili_common.rpc.lottery`，路由键前缀见 `bili_common.rpc.base`。

弱依赖：RPC 超时 / 未连接 / 查询失败时返回 `False`（按不存在处理）。
"""

from bili_common.models.response import StandardResponse
from bili_common.rpc.base import routing_key_for, RpcMethodName
from bili_common.rpc.client import RpcClient
from bili_common.rpc.lottery import (
    CheckLotteryExistRpcParams,
    CheckLotteryExistRpcResult,
    LotteryDetailItem,
    CheckOthersLotDynExistRpcParams,
    CheckOthersLotDynExistRpcResult,
    OthersLotDynDetailItem,
)
from loguru import logger

from app.core.config import settings


class LotteryRpcClient:
    """抽奖资源 RPC 客户端（封装 RpcClient，弱依赖降级）。"""

    def __init__(self) -> None:
        self._client = RpcClient(settings.rabbitmq_url)

    @property
    def connected(self) -> bool:
        return self._client.connected

    async def connect(self) -> None:
        await self._client.connect()

    async def close(self) -> None:
        await self._client.close()

    async def _check(self, lottery_ids: int | str | list[int] | list[str]) -> CheckLotteryExistRpcResult | None:
        """调 check_lottery_exist RPC，返回解析后的结果；失败/未连接返回 None（弱依赖降级）。

        `lottery_ids` 为 int/str 时按单个 ID 处理（统一 `int()` 转换，防止字符串被
        误当可迭代对象拆成单个字符导致查错）；为 list 时批量查询（一次 RPC）。
        """
        # 兼容调用方传字符串 ID（如 RESOURCE 节点 / 收藏请求的 bizId 均为 str）：
        # 字符串必须整体转成一个 int 单查，不能迭代拆分
        if isinstance(lottery_ids, (int, str)):
            single = True
            ids = [int(lottery_ids)]
        else:
            single = False
            ids = [int(x) for x in dict.fromkeys(lottery_ids)]
        routing_key = routing_key_for(RpcMethodName.CHECK_LOTTERY_EXIST)
        payload = (
            CheckLotteryExistRpcParams(lottery_id=ids[0]).model_dump()
            if single
            else CheckLotteryExistRpcParams(lottery_ids=ids).model_dump()
        )
        try:
            if not self._client.connected:
                logger.warning("[LotteryRpcClient] 抽奖 RPC 未连接，按不存在处理")
                return None
            raw = await self._client.call(routing_key, payload, timeout=5.0)
        except TimeoutError:
            logger.warning(f"[LotteryRpcClient] check_lottery_exist 超时: {ids}")
            return None
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[LotteryRpcClient] check_lottery_exist 调用失败: {e}")
            return None
        try:
            resp = StandardResponse.model_validate(raw)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[LotteryRpcClient] check_lottery_exist 响应解析失败: {e}")
            return None
        if resp.code != 0 or resp.data is None:
            return None
        try:
            return CheckLotteryExistRpcResult.model_validate(resp.data)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[LotteryRpcClient] check_lottery_exist 结果解析失败: {e}")
            return None

    async def lottery_exists(self, lottery_id: int) -> bool:
        """校验 lottery 是否存在（弱依赖：RPC 失败 / 未连接返回 False）。

        Returns:
            True=存在；False=不存在或校验不可用（调用方按资源不存在处理）。
        """
        result = await self._check(lottery_id)
        return bool(result and result.exists)

    async def get_lottery_detail(self, lottery_id: int) -> dict[str, str | None] | None:
        """实时获取单个 lottery attach 卡片详情（2.20.1）。

        返回 ``{name, cover, jumpUrl}``；资源不存在或 RPC 失败（弱依赖）返回 None，
        调用方保留原节点降级。
        """
        result = await self._check(lottery_id)
        if result is None or not result.exists:
            return None
        return {
            "name": result.title or None,
            "cover": result.cover or None,
            "jumpUrl": result.jumpUrl or None,
        }

    async def get_existing_lottery_ids(self, lottery_ids: list[int]) -> set[int] | None:
        """批量校验 lottery 是否存在（2.23.1 防乱调；2.63.0 起仅服务浏览计数准入）。

        调用方为 `GET /community/interaction/status/{bizId}` 的浏览投递前校验
        （纯读的批量 status 接口不再校验，见计划书 §5.11）。
        

        Returns:
            ``set[存在的 lottery_id]``：RPC 成功（缺失的不在集合内）；
            ``None``：RPC 失败 / 未连接（弱依赖，**校验不可用**——调用方应降级放行，
            不可将失败误判为"全部不存在"）。
        """
        if not lottery_ids:
            return set()
        result = await self._check(lottery_ids)
        if result is None or not result.items:
            return None
        return {item.lottery_id for item in result.items if item.exists}

    async def get_lottery_details(self, lottery_ids: list[int]) -> dict[int, dict[str, str | None]]:
        """批量实时获取 lottery attach 卡片详情（2.20.1，Feed 页一次 RPC）。

        Returns:
            ``{lottery_id: {name, cover, jumpUrl}}``；不存在的 lottery 不放入结果，
            RPC 失败 / 未连接（弱依赖）返回空 dict，调用方保留原节点降级。
        """
        if not lottery_ids:
            return {}
        result = await self._check(lottery_ids)
        if result is None or not result.items:
            return {}
        details: dict[int, dict[str, str | None]] = {}
        for item in result.items:
            if not item.exists:
                continue
            details[item.lottery_id] = {
                "name": item.title or None,
                "cover": item.cover or None,
                "jumpUrl": item.jumpUrl or None,
            }
        return details


    # ==================== 第三方抽奖动态（2.61.0）====================

    async def _check_others(
        self, dyn_ids: int | str | list[int] | list[str]
    ) -> CheckOthersLotDynExistRpcResult | None:
        """调 check_others_lot_dyn_exist RPC；失败 / 未连接返回 None（弱依赖降级）。

        与 `_check` 同构：单值（int/str）按单个 dynId 处理，列表批量查询（一次 RPC）。
        """
        if isinstance(dyn_ids, (int, str)):
            single = True
            ids = [int(dyn_ids)]
        else:
            single = False
            ids = [int(x) for x in dict.fromkeys(dyn_ids)]
        routing_key = routing_key_for(RpcMethodName.CHECK_OTHERS_LOT_DYN_EXIST)
        payload = (
            CheckOthersLotDynExistRpcParams(dyn_id=ids[0]).model_dump()
            if single
            else CheckOthersLotDynExistRpcParams(dyn_ids=ids).model_dump()
        )
        try:
            if not self._client.connected:
                logger.warning("[LotteryRpcClient] 抽奖 RPC 未连接，按不存在处理")
                return None
            raw = await self._client.call(routing_key, payload, timeout=5.0)
        except TimeoutError:
            logger.warning(f"[LotteryRpcClient] check_others_lot_dyn_exist 超时: {ids}")
            return None
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[LotteryRpcClient] check_others_lot_dyn_exist 调用失败: {e}")
            return None
        try:
            resp = StandardResponse.model_validate(raw)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[LotteryRpcClient] check_others_lot_dyn_exist 响应解析失败: {e}")
            return None
        if resp.code != 0 or resp.data is None:
            return None
        try:
            return CheckOthersLotDynExistRpcResult.model_validate(resp.data)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[LotteryRpcClient] check_others_lot_dyn_exist 结果解析失败: {e}")
            return None

    async def others_lot_dyn_exists(self, dyn_id: int) -> bool:
        """校验第三方抽奖动态是否存在（弱依赖：RPC 失败 / 未连接返回 False）。"""
        result = await self._check_others(dyn_id)
        return bool(result and result.exists)

    async def get_existing_others_lot_dyn_ids(self, dyn_ids: list[int]) -> set[int] | None:
        """批量校验第三方抽奖动态是否存在（2.63.0 起仅服务浏览计数准入）。

        Returns:
            ``set[存在的 dynId]``：RPC 成功；``None``：RPC 失败 / 未连接（弱依赖，
            调用方应降级放行，不可将失败误判为「全部不存在」）。
        """
        if not dyn_ids:
            return set()
        result = await self._check_others(dyn_ids)
        if result is None or not result.items:
            return None
        return {item.dyn_id for item in result.items if item.exists}

    async def get_others_lot_dyn_details(
        self, dyn_ids: list[int]
    ) -> dict[int, dict[str, object | None]]:
        """批量获取第三方抽奖动态详情（标题 / 作者 / 跳转链接）。

        不存在的 dynId 不放入结果；RPC 失败（弱依赖）返回空 dict，调用方保留原节点降级。
        """
        if not dyn_ids:
            return {}
        result = await self._check_others(dyn_ids)
        if result is None or not result.items:
            return {}
        details: dict[int, dict[str, object | None]] = {}
        for item in result.items:
            if not item.exists:
                continue
            details[item.dyn_id] = {
                "name": item.title,
                "cover": item.cover,
                "jumpUrl": item.jumpUrl,
                "authorMid": item.authorMid,
                "authorName": item.authorName,
            }
        return details


# 全局单例
_rpc_client = LotteryRpcClient()


async def get_lottery_rpc_client() -> LotteryRpcClient:
    """获取全局抽奖 RPC 客户端单例。"""
    if not _rpc_client.connected:
        try:
            await _rpc_client.connect()
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[LotteryRpcClient] 连接失败（弱依赖，降级）: {e}")
    return _rpc_client


__all__ = ["LotteryRpcClient", "get_lottery_rpc_client"]
