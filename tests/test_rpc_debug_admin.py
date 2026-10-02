"""管理端「RPC 调试」接口测试（无需真实 RabbitMQ / DB）。

覆盖：
- 契约注册表：各 RPC 模块的方法都被登记，路由键与前缀函数一致；
- 参数校验：JSON 解析失败 / 不符合契约 / 方法未登记，都在 HTTP 边界被拦下（不投递 MQ）；
- 真实调用回显：`RpcClient.call` 打桩，验证信封原样回显与耗时统计；
- 鉴权：非 root 必须 403。

运行（在 be-message-service 目录下）::

    uv run pytest tests/test_rpc_debug_admin.py -v
"""

import json
from unittest.mock import AsyncMock, patch

import pytest
from bili_common.rpc.base import (
    geoip_rpc_routing_key_for,
    pptr_routing_key_for,
    rpa_rpc_routing_key_for,
)
from bili_common.rpc.geoip import GeoIpRpcMethodName
from bili_common.rpc.pptr_user import RpcMethodName as PptrMethodName
from bili_common.rpc.rpa import RpaRpcMethodName
from httpx import ASGITransport, AsyncClient

import app.main
from app.main import app
from app.services.admin.rpc_debug import RpcDebugService

# ---------------------------------------------------------------------------
# 服务层：契约注册表
# ---------------------------------------------------------------------------


def test_registry_contains_methods_from_every_contract():
    """各 RPC 模块的契约方法都必须进入注册表（新增契约漏登记会被这里拦下）。"""
    names = {m.method_name for m in RpcDebugService.methods()}

    # be-message 服务端
    assert str(PptrMethodName.GET_USER_CARD) in names
    assert str(PptrMethodName.GET_USER_NAV) in names
    assert GeoIpRpcMethodName.RESOLVE_IP_REGION in names
    # rpa / lottery（be-message 作为客户端）
    assert str(RpaRpcMethodName.LIST_TAGS) in names
    assert "get_reserve_lottery" in names


def test_registry_routing_keys_match_prefix_functions():
    """路由键必须由 bili_common 前缀函数推导（不允许手工拼写）。"""
    resolved = RpcDebugService.resolve(str(PptrMethodName.GET_USER_CARD))
    assert resolved is not None
    routing_key, _params_model = resolved
    assert routing_key == pptr_routing_key_for(PptrMethodName.GET_USER_CARD)

    resolved = RpcDebugService.resolve(GeoIpRpcMethodName.RESOLVE_IP_REGION)
    assert resolved is not None
    assert resolved[0] == geoip_rpc_routing_key_for(GeoIpRpcMethodName.RESOLVE_IP_REGION)

    resolved = RpcDebugService.resolve(str(RpaRpcMethodName.LIST_TAGS))
    assert resolved is not None
    assert resolved[0] == rpa_rpc_routing_key_for(RpaRpcMethodName.LIST_TAGS)


def test_resolve_unknown_method_returns_none():
    """未登记的方法必须解析失败（调试接口不允许任意路由键）。"""
    assert RpcDebugService.resolve("not_a_real_method") is None
    # 现成队列名 ≠ 可测方法：防把调试接口当 MQ 发布器用
    assert RpcDebugService.resolve("message.push") is None


# ---------------------------------------------------------------------------
# 服务层：参数校验
# ---------------------------------------------------------------------------


def test_validate_payload_ok():
    """合法 JSON → 返回契约 params 模型实例。"""
    params = RpcDebugService.validate_payload(
        str(PptrMethodName.GET_USER_CARD), '{"uid": 12345}'
    )
    assert params.uid == 12345


def test_validate_payload_rejects_bad_json():
    with pytest.raises(ValueError, match="不是合法 JSON"):
        RpcDebugService.validate_payload(str(PptrMethodName.GET_USER_CARD), "{bad json")


def test_validate_payload_rejects_non_object():
    with pytest.raises(TypeError, match="JSON 对象"):
        RpcDebugService.validate_payload(str(PptrMethodName.GET_USER_CARD), "[1, 2]")


def test_validate_payload_rejects_contract_mismatch():
    """字段类型不符契约 → ValueError（消息带模型名），坏消息不进 MQ。"""
    with pytest.raises(ValueError, match="PptrGetUserCardParams"):
        RpcDebugService.validate_payload(
            str(PptrMethodName.GET_USER_CARD), '{"uid": {"nested": true}}'
        )


def test_validate_payload_unknown_method():
    with pytest.raises(KeyError):
        RpcDebugService.validate_payload("not_a_real_method", "{}")


# ---------------------------------------------------------------------------
# HTTP 层：路由 + 鉴权
# ---------------------------------------------------------------------------


_ROOT_HEADERS = {"x-bili-mid": "1", "x-bili-role": "root"}
_NORMAL_HEADERS = {"x-bili-mid": "2", "x-bili-role": "normal"}


def _client(headers: dict[str, str]) -> AsyncClient:
    """走**真实鉴权链路**（x-bili-* 头 → get_current_user → get_admin_user 角色检查）。"""
    return AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test", headers=headers
    )


async def test_methods_endpoint_lists_contracts():
    async with _client(_ROOT_HEADERS) as ac:
        resp = await ac.get("/api/v1/message/admin/rpc-debug/methods")
    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == 0
    items = body["data"]
    assert items, "可测试方法列表为空"
    one = items[0]
    assert {
        "method_name",
        "server",
        "routing_key",
        "params_model",
        "params_schema_json",
    } <= set(one)
    # params_schema_json 必须是合法 JSON 文本（前端 JSON.parse 后渲染表单）
    schema = json.loads(one["params_schema_json"])
    assert "properties" in schema


async def test_invoke_returns_envelope_from_rpc_client():
    """成功路径：RpcClient.call 打桩，验证信封原样回显 + 路由键正确。"""
    fake_envelope = {"code": 0, "msg": "success", "data": {"uid": 1}}
    with patch.object(
        RpcDebugService, "_client", AsyncMock()
    ) as mock_client, patch.object(
        RpcDebugService, "ensure_connected", new=AsyncMock()
    ):
        mock_client.call = AsyncMock(return_value=fake_envelope)
        async with _client(_ROOT_HEADERS) as ac:
            resp = await ac.post(
                "/api/v1/message/admin/rpc-debug/invoke",
                json={
                    "method_name": str(PptrMethodName.GET_USER_CARD),
                    "payload_json": '{"uid": 1}',
                },
            )

    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == 0
    data = body["data"]
    assert data["routing_key"] == pptr_routing_key_for(PptrMethodName.GET_USER_CARD)
    assert data["duration_ms"] >= 0
    assert data["reply"] == fake_envelope


async def test_invoke_unknown_method_is_404_envelope():
    async with _client(_ROOT_HEADERS) as ac:
        resp = await ac.post(
            "/api/v1/message/admin/rpc-debug/invoke",
            json={"method_name": "not_a_real_method", "payload_json": "{}"},
        )
    body = resp.json()
    assert body["code"] == 404
    assert "未知 RPC 方法" in body["msg"]


async def test_invoke_invalid_payload_is_400_envelope():
    """参数不合法必须 400 回包（而不是投递坏消息让调用方等超时）。"""
    async with _client(_ROOT_HEADERS) as ac:
        resp = await ac.post(
            "/api/v1/message/admin/rpc-debug/invoke",
            json={
                "method_name": str(PptrMethodName.GET_USER_CARD),
                "payload_json": '{"uid": {"nested": true}}',
            },
        )
    body = resp.json()
    assert body["code"] == 400
    assert "PptrGetUserCardParams" in body["msg"]


async def test_invoke_requires_root():
    """非 root 必须 403（调试接口可触达写方法，不能放开给普通管理员）。"""
    async with _client(_NORMAL_HEADERS) as ac:
        resp = await ac.get("/api/v1/message/admin/rpc-debug/methods")
    assert resp.status_code == 403
