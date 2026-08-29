"""系统通知 RPC 服务端契约测试（2.48.0，无需真实 RabbitMQ / DB）。

覆盖：
- 路由键 / 方法名 / 契约映射（`bili_common.rpc.notify`）；
- `rpc_publish_notify` 成功发布、幂等命中、无返回记录三种回包；
- 与服务层 `NotifyService.create_idempotent` 的衔接（传参正确）。

运行（在 be-message-service 目录下）::

    uv run pytest tests/test_rpc_notify.py -v
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from bili_common.models import (
    NotifyRpcMethodName,
    PublishNotifyParams,
    PublishNotifyResult,
    notify_rpc_routing_key_for,
)
from bili_common.rpc.base import NOTIFY_RPC_ROUTING_KEY_PREFIX
from bili_common.rpc.notify import NOTIFY_RPC_CONTRACT

# 先加载 app.main，建立可用的导入顺序（避免 app.core.broker ↔ app.mq.router 的
# 既有循环导入），再按需 import app.mq.rpc_notify。
import app.main  # noqa: F401
from app.models.enums import NotifyLevelEnum, NotifyTargetTypeEnum
from app.models.schemas import NotifyCreateReq
from app.mq.rpc_notify import rpc_publish_notify


def test_routing_key_and_contract():
    """路由键与契约映射：两端共用同一份公共库定义。"""
    assert NOTIFY_RPC_ROUTING_KEY_PREFIX == "message.notify.rpc"
    assert (
        notify_rpc_routing_key_for(NotifyRpcMethodName.PUBLISH_NOTIFY)
        == "message.notify.rpc.publish_notify"
    )
    params_model, result_model = NOTIFY_RPC_CONTRACT[
        NotifyRpcMethodName.PUBLISH_NOTIFY
    ]
    assert params_model is PublishNotifyParams
    assert result_model is PublishNotifyResult


def _params(**kwargs) -> PublishNotifyParams:
    defaults: dict[str, object] = {
        "title": "欢迎加入，爱丽丝！",
        "content": "Hi，爱丽丝！",
        "target_type": NotifyTargetTypeEnum.CUSTOM,
        "target_value": "12345",
        "creator_mid": 0,
    }
    defaults.update(kwargs)
    return PublishNotifyParams(**defaults)


def _fake_session_ctx():
    """构造一个可被 `async with new_session() as s` 使用的假会话上下文。"""
    session = MagicMock()
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=session)
    ctx.__aexit__ = AsyncMock(return_value=False)
    return ctx


def test_rpc_publish_notify_success():
    """成功发布：回包 code=0，data 带 notify_id。"""
    with (
        patch("app.mq.rpc_notify.new_session", return_value=_fake_session_ctx()),
        patch(
            "app.mq.rpc_notify.NotifyService.create_idempotent",
            new=AsyncMock(return_value=(MagicMock(id=9), False)),
        ) as mock_create,
    ):
        resp = asyncio.run(rpc_publish_notify(_params()))

    assert resp.code == 0
    assert resp.data.notify_id == 9
    assert resp.data.duplicated is False

    _, creator_mid, req = mock_create.await_args.args
    assert creator_mid == 0
    assert isinstance(req, NotifyCreateReq)
    assert req.target_type == NotifyTargetTypeEnum.CUSTOM
    assert req.target_value == "12345"
    assert req.level == NotifyLevelEnum.NORMAL
    assert req.publish_now is True


def test_rpc_publish_notify_duplicated():
    """幂等命中：duplicated=True，且不重复写库。"""
    with (
        patch("app.mq.rpc_notify.new_session", return_value=_fake_session_ctx()),
        patch(
            "app.mq.rpc_notify.NotifyService.create_idempotent",
            new=AsyncMock(return_value=(MagicMock(id=9), True)),
        ),
    ):
        resp = asyncio.run(rpc_publish_notify(_params()))

    assert resp.code == 0
    assert resp.data.notify_id == 9
    assert resp.data.duplicated is True


def test_rpc_publish_notify_no_item():
    """服务层未返回通知记录：回包失败（不走 success）。"""
    with (
        patch("app.mq.rpc_notify.new_session", return_value=_fake_session_ctx()),
        patch(
            "app.mq.rpc_notify.NotifyService.create_idempotent",
            new=AsyncMock(return_value=(None, False)),
        ),
    ):
        resp = asyncio.run(rpc_publish_notify(_params()))

    assert resp.code != 0
