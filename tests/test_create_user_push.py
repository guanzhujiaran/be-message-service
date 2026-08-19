"""创建新用户后推送站内系统消息的测试（无需真实 RabbitMQ / DB）。

覆盖：
- `_push_new_user_notify` 成功路径：调用 `NotifyService.send_to_user`，
  传入正确的 mid（新用户 uid）/ title / content；
- 推送失败路径：`NotifyService.send_to_user` 抛异常时，函数不向上抛（弱依赖），
  仅记录日志。

运行（在 be-message-service 目录下）::

    uv run pytest tests/test_create_user_push.py -v
"""

import asyncio
from unittest.mock import AsyncMock, patch

# 先加载 app.main，建立可用的导入顺序（避免 app.core.broker ↔ app.mq.router 的
# 既有循环导入），再按需 import app.mq.rpc_pptr_user。
import app.main  # noqa: F401
from app.mq.rpc_pptr_user import _push_new_user_notify


def test_push_new_user_notify_success():
    """成功路径：正确调用 NotifyService.send_to_user 并传入参数。"""
    with patch(
        "app.mq.rpc_pptr_user.NotifyService.send_to_user", new=AsyncMock()
    ) as mock_send:
        asyncio.run(
            _push_new_user_notify(user_name="alice", uid=12345, uname="爱丽丝")
        )

    mock_send.assert_awaited_once()
    kwargs = mock_send.await_args.kwargs
    assert kwargs["mid"] == 12345  # 站内系统消息定向到新用户 uid
    assert kwargs["title"] == "[新用户注册] alice"
    assert "alice" in kwargs["content"]
    assert "12345" in kwargs["content"]
    assert "爱丽丝" in kwargs["content"]


def test_push_new_user_notify_failure_swallowed():
    """失败路径：send_to_user 抛异常时不向上抛（弱依赖）。"""
    with patch(
        "app.mq.rpc_pptr_user.NotifyService.send_to_user",
        new=AsyncMock(side_effect=RuntimeError("db down")),
    ):
        # 弱依赖：站内通知写入失败不应抛出异常
        asyncio.run(_push_new_user_notify(user_name="bob", uid=2))
