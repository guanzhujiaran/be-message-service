"""RPC 注册接线（wiring）回归测试：无需真实 RabbitMQ / DB。

为什么单独测「接线」：
    既有 `test_rpc_notify.py` / `test_push_rpc.py` 都是**直接调用 handler 函数**，
    只覆盖业务逻辑；它们无法发现「消息体根本没被 decode 成 params 模型」这类
    **注册方式**问题。2026-10-02 线上真实事故正是此类：

        ERROR RPC rpc_resolve_ip_region 失败:
        AttributeError: 'RabbitMessage' object has no attribute 'ip'

根因：be-message 的 broker 由 FastAPI 集成的 `RabbitRouter` 创建，FD 配置带
`get_dependent=get_fastapi_dependant`。FastStream 在 `FastDependsConfig.build_call`
里对「有 get_dependent」的情况**不会**再做「decode 消息体 → 单参数注入」的包装，
该行为改由 `router.subscriber` 挂上的 FastAPI 兼容装饰器（`_call_decorators`）负责。
一旦误用 `broker.subscriber`，装饰器链为空，handler 直接收到原始 `RabbitMessage`。

本文件覆盖：
- 接线守卫：所有 `rpc_*` subscriber 必须挂上 FastAPI 兼容装饰器；
- 运行时往返：经真实注册链路喂入 RabbitMessage(JSON body)，断言 handler
  收到的是解码后的 params 模型、回包是 StandardResponse（geoip / push 两例）；
- 反证：剥掉兼容装饰器（等价于 `@broker.subscriber`）后必须复现原报错，
  保证本测试确实能挡住这次回归。

运行（在 be-message-service 目录下）::

    uv run pytest tests/test_rpc_wiring.py -v
"""

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from faststream.rabbit import TestRabbitBroker
from faststream.rabbit.message import RabbitMessage

# 先加载 app.main，建立可用的导入顺序（避免 app.core.broker ↔ app.mq.router 的
# 既有循环导入），再按需 import app.mq.*。
import app.main  # noqa: F401
from app.core.broker import broker, message_exchange
from app.mq.rpc_geoip import rpc_resolve_ip_region  # noqa: F401  触发注册 + 供守卫定位
from app.services.infrastructure.geo_ip import GeoIpResult

# 真实路由键（与各 RPC 模块注册时用的完全一致）
GEOIP_RK = "message.geoip.rpc.resolve_ip_region"
PUSH_RK = "message.push.rpc.push_message"

# 注册瞬间的 `rpc_safe` 包装函数：`HandlerCallWrapper._original_call` 在 broker 启动
# （`_build_fastdepends_model()`）后会被替换成 FastAPI 兼容包装，故此处 import 期先取一份。
# 它是反证用例需要的「未挂兼容装饰器」的原始 handler。
_GEOIP_HANDLER_AT_IMPORT = rpc_resolve_ip_region._original_call
assert "args" in _GEOIP_HANDLER_AT_IMPORT.__code__.co_varnames, (
    "rpc_safe 包装形态变了（不再是 (*args, **kwargs)），请同步更新反证用例"
)


def _rpc_subscribers() -> dict[str, Any]:
    """收集所有 RPC subscriber：{handler 函数名: subscriber}。

    handler 名取自 `HandlerItem.name`（内部会对 `rpc_safe` / FastAPI 兼容包装做
    `inspect.unwrap`，最终拿到底层 `rpc_xxx` 函数名），据此与普通消费者区分。
    """
    found: dict[str, Any] = {}
    for sub in broker.subscribers:
        for call in sub.calls:
            if call.name.startswith("rpc_"):
                found.setdefault(call.name, sub)
    return found


def _make_message(body: dict[str, Any], *, sub: Any) -> RabbitMessage:
    """构造一条「和 RabbitMQ 真消息等价」的 RabbitMessage（JSON body）。

    raw_message 用 MagicMock 占位（本测试不触发 ack/nack）；
    decoder 直接复用 subscriber 自己解析出的那一个，保证与线上解码路径一致。
    """
    msg = RabbitMessage(
        raw_message=MagicMock(),
        body=json.dumps(body).encode(),
        content_type="application/json",
        reply_to="amq.rabbitmq.reply-to",
    )
    msg.set_decoder(sub._decoder)
    return msg


def _reply_body(result: Any) -> dict[str, Any]:
    """把 handler 返回值归一成 dict。

    FastAPI 兼容链路返回 `faststream.response.Response`（body 即回包内容），
    纯 FastDepends 链路返回 `StandardResponse`；两种都归一成 dict 便于断言。
    """
    payload = getattr(result, "body", result)
    if hasattr(payload, "model_dump"):
        return payload.model_dump()
    return payload


# ---------------------------------------------------------------------------
# 1) 接线守卫：所有 RPC subscriber 都必须走 router.subscriber
# ---------------------------------------------------------------------------


def test_all_rpc_subscribers_have_fastapi_compat_decorator():
    """回归守卫：任何 `rpc_*` subscriber 都必须挂 FastAPI 兼容装饰器。

    `_call_decorators` 是 FastStream 内部字段，这里刻意依赖它——它正是
    `router.subscriber` 与 `broker.subscriber` 在行为上唯一的差异点：
    为空即代表 handler 会收到原始 RabbitMessage（本次线上事故的形态）。
    """
    rpc_subs = _rpc_subscribers()
    assert rpc_subs, "未发现任何 rpc_* subscriber，守卫测试失效（注册入口变了？）"

    missing = [name for name, sub in rpc_subs.items() if not sub._call_decorators]
    assert not missing, (
        f"以下 RPC subscriber 未挂 FastAPI 兼容装饰器（很可能误用了 @broker.subscriber，"
        f"应改为 @router.subscriber）: {missing}"
    )


def test_rpc_subscribers_bind_expected_routing_keys():
    """队列名（= routing_key）必须与客户端 `broker.request(queue=routing_key)` 一致。"""
    rpc_subs = _rpc_subscribers()
    assert rpc_subs["rpc_resolve_ip_region"].queue.name == GEOIP_RK
    assert rpc_subs["rpc_push_message"].queue.name == PUSH_RK


# ---------------------------------------------------------------------------
# 2) 运行时往返：经真实注册链路喂消息，断言 params 被正确解码
# ---------------------------------------------------------------------------


async def test_geoip_rpc_receives_decoded_params():
    """geoip RPC：body={"ip": ...} → handler 收到 ResolveIpRegionParams 并正常回包。

    修复前该用例会失败：handler 拿到 RabbitMessage，`params.ip` 抛 AttributeError，
    被 rpc_safe 兜底成 code=500，回包里出现 "AttributeError"。
    """
    sub = _rpc_subscribers()["rpc_resolve_ip_region"]
    sub._build_fastdepends_model()

    fake_geo = GeoIpResult(poi="浙江 杭州", lat=30.1, lng=120.2, isp="中国电信")
    with patch("app.mq.rpc_geoip.lookup", return_value=fake_geo) as mock_lookup:
        body = _reply_body(
            await sub.calls[0].handler.call_wrapped(
                _make_message({"ip": "114.114.114.114"}, sub=sub)
            )
        )

    # 关键断言 1：handler 确实收到了「解码后的 str」，而不是 RabbitMessage
    mock_lookup.assert_called_once_with("114.114.114.114")
    # 关键断言 2：回包是成功的 StandardResponse（不是 rpc_safe 兜底的 500）
    assert body["code"] == 0, body
    assert body["data"] == {"region": "浙江 杭州", "isp": "中国电信"}


async def test_geoip_rpc_request_reply_roundtrip():
    """RPC 请求 / 响应整链路：客户端 request → 服务端 handler → 回包到 reply_to。

    用 FastStream 内存 TestRabbitBroker 跑真实 `broker.request()`（Direct Reply-To），
    这是唯一一条能同时验证「参数注入」+「回包发出」的用例：handler 报错被 rpc_safe
    兜成 code=500 时，客户端拿到的是错误信封而不是超时，这里一并覆盖。

    注：真实客户端是 `broker.request(payload, queue=routing_key)`，走**默认 exchange**
    按「队列名 = routing_key」投递；内存 broker 不模拟该行为，故这里显式指定 exchange，
    消费 / 回包路径与线上一致。
    """
    fake_geo = GeoIpResult(poi="浙江 杭州", lat=30.1, lng=120.2, isp="中国电信")
    with patch("app.mq.rpc_geoip.lookup", return_value=fake_geo):
        async with TestRabbitBroker(broker) as br:
            resp = await br.request(
                {"ip": "114.114.114.114"},
                queue=GEOIP_RK,
                exchange=message_exchange,
                timeout=5.0,
            )

    body = json.loads(resp.body.decode())
    assert body["code"] == 0, body
    assert body["data"] == {"region": "浙江 杭州", "isp": "中国电信"}


async def test_push_rpc_receives_decoded_params():
    """push RPC：同一接线问题会同时命中所有 RPC，这里再验一例。

    handler 内部的 broker.publish 用 AsyncMock 顶掉（不需要真实连接），
    只验证「请求体 → PushRpcSendParams」这一步接线是否通。
    """
    sub = _rpc_subscribers()["rpc_push_message"]
    sub._build_fastdepends_model()

    with patch.object(broker, "publish", new=AsyncMock()) as mock_publish:
        body = _reply_body(
            await sub.calls[0].handler.call_wrapped(
                _make_message(
                    {"title": "标题", "content": "正文", "user_label": "用户 1"},
                    sub=sub,
                )
            )
        )

    assert body["code"] == 0, body
    # _with_label 把来源标签拼进标题，证明 params.title / params.user_label 已解析
    assert body["data"]["title"] == "[用户 1] 标题"
    assert mock_publish.await_count == 1


# ---------------------------------------------------------------------------
# 3) 反证：剥掉兼容装饰器后必须复现线上报错（确保守卫真的有效）
# ---------------------------------------------------------------------------


async def test_broker_subscriber_style_registration_reproduces_bug():
    """把兼容装饰器去掉（等价 `@broker.subscriber`）→ 必须复现原报错。

    这条用例不是「测框架」，而是给守卫测试做对照：它证明第 1 条守卫拦下的
    确实就是会导致 handler 收到 RabbitMessage 的那类注册。
    """
    sub = _rpc_subscribers()["rpc_resolve_ip_region"]
    cfg = sub._outer_config.fd_config

    # 刻意不传 call_decorators，模拟 @broker.subscriber 的注册方式
    built = cfg.build_call(_GEOIP_HANDLER_AT_IMPORT, call_decorators=())
    msg = _make_message({"ip": "114.114.114.114"}, sub=sub)

    resp = await built.wrapped_call(msg)

    assert resp.code != 0
    assert "RabbitMessage" in resp.msg
    assert "has no attribute 'ip'" in resp.msg
