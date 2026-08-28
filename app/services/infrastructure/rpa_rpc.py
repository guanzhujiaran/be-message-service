"""RPA 资源 RPC 客户端（be-message 侧，2.18.0 新增）。

be-message 作为 RPC 客户端，经 `message.rpa.rpc.get_resource_detail` 同步调用
RPA-Browser 服务端，获取 RPA 资源（action / workflow / plugin / browser）详情，
供互动状态等接口随 `bizType`+`bizId` 一并返回前端。

契约（方法名 / 参数 / 响应）见 `bili_common.rpc.rpa`，路由键前缀见 `bili_common.rpc.base`。

弱依赖：RPC 超时 / 未连接 / 资源不存在时返回 `None`，不影响互动主流程。
"""

from bili_common.models.response import StandardResponse
from bili_common.rpc.base import rpa_rpc_routing_key_for
from bili_common.rpc.client import RpcClient
from bili_common.rpc.rpa import (
    GetResourceDetailParams,
    GetResourceDetailResult,
    HideResourceParams,
    HideResourceResult,
    RpaRpcMethodName,
)
from loguru import logger

from app.core.config import settings


class RpaRpcClient:
    """RPA 资源 RPC 客户端（封装 RpcClient，弱依赖降级）。"""

    def __init__(self) -> None:
        self._client = RpcClient(settings.rabbitmq_url)

    @property
    def connected(self) -> bool:
        return self._client.connected

    async def connect(self) -> None:
        await self._client.connect()

    async def close(self) -> None:
        await self._client.close()

    async def get_resource_detail(self, biz_type: str, biz_id: int) -> GetResourceDetailResult | None:
        """获取 RPA 资源详情（弱依赖：失败返回 None，不抛错）。

        Returns:
            GetResourceDetailResult{detail}；RPC 失败 / 资源不存在返回 None。
        """
        routing_key = rpa_rpc_routing_key_for(RpaRpcMethodName.GET_RESOURCE_DETAIL)
        payload = GetResourceDetailParams(bizType=biz_type, bizId=biz_id).model_dump()
        try:
            if not self._client.connected:
                logger.warning("[RpaRpcClient] RPA RPC 未连接，跳过资源详情获取")
                return None
            raw = await self._client.call(routing_key, payload, timeout=5.0)
        except TimeoutError:
            logger.warning(f"[RpaRpcClient] get_resource_detail 超时: {biz_type}/{biz_id}")
            return None
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[RpaRpcClient] get_resource_detail 调用失败: {e}")
            return None
        try:
            resp = StandardResponse.model_validate(raw)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[RpaRpcClient] get_resource_detail 响应解析失败: {e}")
            return None
        if resp.code != 0 or resp.data is None:
            return None
        return GetResourceDetailResult.model_validate(resp.data)


    async def hide_resource(
        self,
        *,
        biz_type: str,
        biz_id: int,
        operator_mid: int,
        reason: str = "",
    ) -> bool:
        """举报处置：通知归属服务下架资源（2.39.0，弱依赖）。

        归属服务（RPA-Browser）按 ``biz_type`` 内部路由（lottery→crawler、
        rpa_*→本地）。RPC 超时 / 未连接 / 失败返回 False（仅告警，不抛错）——
        被举报资源已由本地 ``TResourceFeed`` 退出 Feed，RPC 仅为资源层实际下架。
        """
        routing_key = rpa_rpc_routing_key_for(RpaRpcMethodName.HIDE_RESOURCE)
        payload = HideResourceParams(
            bizType=biz_type,
            bizId=biz_id,
            operatorMid=operator_mid,
            reason=reason,
        ).model_dump()
        try:
            if not self._client.connected:
                logger.warning("[RpaRpcClient] RPA RPC 未连接，跳过 hide_resource")
                return False
            raw = await self._client.call(routing_key, payload, timeout=5.0)
        except TimeoutError:
            logger.warning(f"[RpaRpcClient] hide_resource 超时: {biz_type}/{biz_id}")
            return False
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[RpaRpcClient] hide_resource 调用失败: {e}")
            return False
        try:
            resp = StandardResponse.model_validate(raw)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[RpaRpcClient] hide_resource 响应解析失败: {e}")
            return False
        if resp.code != 0 or resp.data is None:
            logger.warning(f"[RpaRpcClient] hide_resource 业务失败: {resp.msg}")
            return False
        result = HideResourceResult.model_validate(resp.data)
        if not result.success:
            logger.warning(
                f"[RpaRpcClient] hide_resource 处置失败: {result.message}"
            )
        return result.success


# 全局单例
rpa_rpc_client = RpaRpcClient()

__all__ = ["RpaRpcClient", "rpa_rpc_client"]
