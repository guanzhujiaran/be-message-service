"""站外推送 RPC 契约与 handler 逻辑测试（无需真实 RabbitMQ）。

覆盖：
- push_rpc_routing_key_for 的路由键生成；
- PushRpcSendParams / PushRpcSendNowParams 请求模型反序列化；
- PushRpcSendResult / PushRpcSendNowResult 响应模型序列化；
- rpc_push._with_label 的来源标签标题前缀逻辑。

运行（在 be-message-service 目录下）::

    uv run pytest tests/test_push_rpc.py -v
"""

# 先加载 app.main，建立可用的导入顺序（避免 app.core.broker ↔ app.mq.router 的
# 既有循环导入），再按需 import app.mq.rpc_push。
import app.main  # noqa: F401
from app.mq.rpc_push import _with_label
from bili_common.models import (
    PushRpcMethodName,
    PushRpcSendNowParams,
    PushRpcSendNowResult,
    PushRpcSendParams,
    PushRpcSendResult,
    push_rpc_routing_key_for,
)


def test_routing_key_generation():
    """路由键应为 message.push.rpc.<method>。"""
    assert (
        push_rpc_routing_key_for(PushRpcMethodName.PUSH_MESSAGE)
        == "message.push.rpc.push_message"
    )
    assert (
        push_rpc_routing_key_for(PushRpcMethodName.SEND_PUSH_NOW)
        == "message.push.rpc.send_push_now"
    )


def test_send_params_roundtrip():
    """push_message 请求参数可反序列化，默认字段生效。"""
    p = PushRpcSendParams(
        title="标题", content="正文", push_type="markdown", user_label="用户 1"
    )
    assert p.title == "标题"
    assert p.content == "正文"
    assert p.push_type == "markdown"
    assert p.user_label == "用户 1"
    assert p.config is None


def test_send_now_params_roundtrip():
    """send_push_now 请求参数可反序列化。"""
    p = PushRpcSendNowParams(title="t", content="c")
    assert p.title == "t"
    assert p.push_type == "text"
    assert p.user_label is None


def test_send_result_serialization():
    """push_message 响应可序列化。"""
    r = PushRpcSendResult(title="[用户 1] 标题", queued=True)
    data = r.model_dump()
    assert data["title"] == "[用户 1] 标题"
    assert data["queued"] is True


def test_send_now_result_serialization():
    """send_push_now 响应可序列化。"""
    r = PushRpcSendNowResult(success=True, message="ok", sent_channels=["pushme"])
    data = r.model_dump()
    assert data["success"] is True
    assert data["sent_channels"] == ["pushme"]


def test_with_label():
    """带来源标签时拼进标题前缀；空标签 / None 时原样返回。"""
    assert _with_label("标题", "用户 1") == "[用户 1] 标题"
    assert _with_label("标题", "  用户 1  ") == "[用户 1] 标题"
    assert _with_label("标题", "") == "标题"
    assert _with_label("标题", None) == "标题"
    assert _with_label("标题", "   ") == "标题"
