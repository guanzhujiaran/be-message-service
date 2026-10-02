"""管理端「RPC 调试」服务（root 专用）。

把 `bili_common.rpc.*` 里已登记的 RPC 契约收敛成一张「可测试方法表」：
管理端选方法 + 贴 JSON 参数 → 本服务用该方法的 params 模型严格校验 →
经 `bili_common.rpc.client.RpcClient`（Direct Reply-To）真实往返 → 回显原始信封。

为什么不做「任意 routing_key」调用：
    调试接口若接受任意路由键，就等价于一个免审核的 MQ 发布器（可投递到任何队列，
    包括 `message.push` 之类真实业务队列）。因此这里只允许调用**契约表登记过**的方法，
    路由键由 `bili_common.rpc.base` 的前缀函数按方法名推导，不接受外部输入。

为什么参数在 HTTP 边界校验：
    `rpc_safe` 只包住 RPC handler 本身；参数非法时 FastAPI 兼容链路的
    `RequestValidationError` 会抛在 handler 之外，FastStream 不回包，调用方只能干等超时。
    这里先本地校验、非法直接 400 回包，坏消息根本不进 MQ。

实现要点：
- `RpcClient` **懒连接**（首次调用时 connect，进程内复用），不在 lifespan 里长连——
  调试是低频操作，不值得为它常驻一条 RabbitMQ 连接；
- 每个方法可声明独立超时（默认 5s，与其它 RPC 客户端一致，上限 30s）。
"""

import json
import time
from typing import Literal

from bili_common.models.response import StandardResponse
from bili_common.rpc.base import (
    geoip_rpc_routing_key_for,
    notify_rpc_routing_key_for,
    pptr_routing_key_for,
    push_rpc_routing_key_for,
    routing_key_for,
    rpa_rpc_routing_key_for,
)
from bili_common.rpc.client import RpcClient
from bili_common.rpc.geoip import GEOIP_RPC_CONTRACT
from bili_common.rpc.lottery import RPC_METHOD_PARAMS_MODEL_MAP
from bili_common.rpc.notify import NOTIFY_RPC_CONTRACT
from bili_common.rpc.pptr_user import PPTR_USER_RPC_CONTRACT
from bili_common.rpc.push import PUSH_RPC_CONTRACT
from bili_common.rpc.rpa import RPA_RPC_CONTRACT
from pydantic import BaseModel, Field
from sqlmodel import SQLModel

from app.core.config import settings

# 默认 / 超时上限（秒）。管理端手动调试不该有长调用，上限防误填。
DEFAULT_TIMEOUT_SEC = 5.0
MAX_TIMEOUT_SEC = 30.0

# 归属系统（仅用于前端展示与定位问题该找哪个服务）
RpcServerType = Literal["be-message", "be-bilibili-crawler", "rpa-browser"]


class RpcDebugMethod(BaseModel):
    """一个可测试的 RPC 方法（供前端下拉与参数表单渲染）。"""

    method_name: str = Field(description="方法名（snake_case，契约表键）")
    server: str = Field(description="提供该 RPC 的服务（归属系统，用于定位问题）")
    routing_key: str = Field(description="实际调用的队列名（= routing_key）")
    params_model: str = Field(description="请求参数模型类名")
    # 参数结构是「随方法变化」的动态 JSON Schema，无法静态建模成 pydantic；
    # 以 JSON 字符串出参（前端 JSON.parse 后渲染表单），避免引入无类型的自由 dict 字段。
    params_schema_json: str = Field(description="请求参数模型的 JSON Schema（JSON 字符串）")


class RpcInvokeResult(BaseModel):
    """一次真实 RPC 调用的结果（回显原始信封，不吞错）。"""

    method_name: str = Field(description="方法名")
    routing_key: str = Field(description="实际调用的队列名")
    duration_ms: int = Field(description="耗时（毫秒，含 MQ 往返）")
    # RPC 服务端返回的原始信封（code/msg/data）。调试工具的价值就在于把
    # 「失败也原样回显」，因此这里不做成功/失败分流。
    reply: StandardResponse = Field(description="RPC 服务端返回的 StandardResponse 信封")


def _build_registry() -> dict[str, tuple[str, RpcServerType, type[SQLModel]]]:
    """汇总各 RPC 契约表 → {method_name: (routing_key, server, params_model)}。

    各契约表的键是 StrEnum（str 子类）或纯字符串，统一按 str 收敛；
    同名方法若在多张表出现，后写覆盖先写（当前无重名）。
    """
    registry: dict[str, tuple[str, RpcServerType, type[SQLModel]]] = {}

    for name, (params_model, _result) in PPTR_USER_RPC_CONTRACT.items():
        registry[str(name)] = (pptr_routing_key_for(name), "be-message", params_model)

    for name, (params_model, _result) in PUSH_RPC_CONTRACT.items():
        registry[str(name)] = (push_rpc_routing_key_for(name), "be-message", params_model)

    for name, (params_model, _result) in NOTIFY_RPC_CONTRACT.items():
        registry[str(name)] = (notify_rpc_routing_key_for(name), "be-message", params_model)

    for name, (params_model, _result) in GEOIP_RPC_CONTRACT.items():
        registry[str(name)] = (geoip_rpc_routing_key_for(name), "be-message", params_model)

    for name, (params_model, _result) in RPA_RPC_CONTRACT.items():
        registry[str(name)] = (rpa_rpc_routing_key_for(name), "rpa-browser", params_model)

    # lottery：只有 params 表（服务端是 be-bilibili-crawler，路由键前缀 FastapiApp.rpc）
    for name, params_model in RPC_METHOD_PARAMS_MODEL_MAP.items():
        registry[str(name)] = (routing_key_for(name), "be-bilibili-crawler", params_model)

    return registry


class RpcDebugService:
    """管理端 RPC 调试：契约注册表 + 真实往返。"""

    # 契约注册表在 import 期构建一次（bili_common 契约是静态代码，不会运行期变化）
    _registry: dict[str, tuple[str, RpcServerType, type[SQLModel]]] = _build_registry()

    _client: RpcClient | None = None

    # ------------------------------------------------------------------ 查询

    @classmethod
    def methods(cls) -> list[RpcDebugMethod]:
        """列出全部可测试方法（按方法名排序，便于前端下拉检索）。"""
        items: list[RpcDebugMethod] = []
        for name in sorted(cls._registry):
            routing_key, server, params_model = cls._registry[name]
            items.append(
                RpcDebugMethod(
                    method_name=name,
                    server=server,
                    routing_key=routing_key,
                    params_model=params_model.__name__,
                    params_schema_json=json.dumps(
                        params_model.model_json_schema(), ensure_ascii=False
                    ),
                )
            )
        return items

    @classmethod
    def resolve(cls, method_name: str) -> tuple[str, type[SQLModel]] | None:
        """按方法名解析 (routing_key, params_model)；未登记返回 None。"""
        entry = cls._registry.get(method_name)
        if entry is None:
            return None
        routing_key, _server, params_model = entry
        return routing_key, params_model

    # ------------------------------------------------------------------ 调用

    @classmethod
    def validate_payload(cls, method_name: str, payload_json: str) -> SQLModel:
        """把 JSON 文本解析并按该方法的 params 模型严格校验。

        Raises:
            ValueError: JSON 解析失败或参数校验失败（消息含具体原因）。
            KeyError: 方法未登记。
        """
        entry = cls.resolve(method_name)
        if entry is None:
            raise KeyError(method_name)
        _routing_key, params_model = entry

        try:
            raw = json.loads(payload_json or "{}")
        except json.JSONDecodeError as e:
            raise ValueError(f"payloadJson 不是合法 JSON: {e}") from e

        if not isinstance(raw, dict):
            raise TypeError(
                f"payloadJson 解析后应为 JSON 对象，实际为 {type(raw).__name__}"
            )

        try:
            return params_model.model_validate(raw)
        except Exception as e:
            raise ValueError(f"参数不符合 {params_model.__name__} 契约: {e}") from e

    @classmethod
    async def invoke(
        cls,
        *,
        method_name: str,
        payload_json: str = "{}",
        timeout: float = DEFAULT_TIMEOUT_SEC,
    ) -> RpcInvokeResult:
        """发起一次真实 RPC 往返并回显原始信封。

        Raises:
            KeyError: 方法未登记。
            ValueError: payloadJson 非法 / 不符合契约。
            TimeoutError: RPC 超时。
            ConnectionError: RPC 客户端未连接（懒连接失败等）。
        """
        entry = cls.resolve(method_name)
        if entry is None:
            raise KeyError(method_name)
        routing_key, _params_model = entry

        params = cls.validate_payload(method_name, payload_json)
        effective_timeout = min(max(timeout, 1.0), MAX_TIMEOUT_SEC)

        await cls.ensure_connected()

        started = time.perf_counter()
        raw = await cls._client.call(
            routing_key, params.model_dump(), timeout=effective_timeout
        )
        duration_ms = int((time.perf_counter() - started) * 1000)

        return RpcInvokeResult(
            method_name=method_name,
            routing_key=routing_key,
            duration_ms=duration_ms,
            reply=StandardResponse.model_validate(raw),
        )

    # ------------------------------------------------------------------ 客户端

    @classmethod
    def _get_client(cls) -> RpcClient:
        """懒创建的共享 RpcClient（进程内复用）。"""
        if cls._client is None:
            cls._client = RpcClient(settings.rabbitmq_url)
        return cls._client

    @classmethod
    async def ensure_connected(cls) -> None:
        """确保底层 RpcClient 已建连（管理端首次调用前由路由层调用）。"""
        if cls._client is None:
            cls._client = RpcClient(settings.rabbitmq_url)
        await cls._client.connect()

    @classmethod
    async def close(cls) -> None:
        """关闭底层连接（进程退出 / 测试清理用）。"""
        if cls._client is not None:
            await cls._client.close()
            cls._client = None


__all__ = [
    "DEFAULT_TIMEOUT_SEC",
    "MAX_TIMEOUT_SEC",
    "RpcDebugMethod",
    "RpcDebugService",
    "RpcInvokeResult",
]
