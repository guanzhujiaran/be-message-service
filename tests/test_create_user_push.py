"""新建用户后发布「欢迎注册」系统通知的测试（无需真实 RabbitMQ / DB）。

覆盖：
- `_push_new_user_notify` 成功路径：走 `NotifyService.create_idempotent`
  （与管理端 HTTP / 对外 RPC `publish_notify` 同一个发布执行体），
  且 `target_type=CUSTOM` 定向到新用户 uid；
- 失败路径：服务层抛异常时不向上抛，返回 False（弱依赖，不阻断创建用户 RPC）；
- `rpc_create_user`：仅 `created=True` 时发布，已存在用户（upsert）不重复打扰。

对外 RPC 服务端见 `tests/test_rpc_notify.py`。

运行（在 be-message-service 目录下）::

    uv run pytest tests/test_create_user_push.py -v
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from bili_common.models import PptrCreateUserParams

# 先加载 app.main，建立可用的导入顺序（避免 app.core.broker ↔ app.mq.router 的
# 既有循环导入），再按需 import app.mq.rpc_pptr_user。
import app.main  # noqa: F401
from app.models.enums import NotifyTargetTypeEnum
from app.mq.rpc_pptr_user import _push_new_user_notify, rpc_create_user


def _fake_session_ctx():
    """构造一个可被 `async with new_session() as s` 使用的假会话上下文。"""
    session = MagicMock()
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=session)
    ctx.__aexit__ = AsyncMock(return_value=False)
    return ctx


def test_push_new_user_notify_success():
    """成功路径：走幂等发布，且 CUSTOM 定向到新用户 uid。"""
    with (
        patch("app.mq.rpc_pptr_user.new_session", return_value=_fake_session_ctx()),
        patch(
            "app.mq.rpc_pptr_user.NotifyService.create_idempotent",
            new=AsyncMock(return_value=(MagicMock(id=1), False)),
        ) as mock_create,
    ):
        ok = asyncio.run(
            _push_new_user_notify(user_name="alice", uid=12345, uname="爱丽丝")
        )

    assert ok is True
    mock_create.assert_awaited_once()
    kwargs = mock_create.await_args.kwargs
    assert kwargs["creator_mid"] == 0  # 系统自动发布，非管理员

    req = kwargs["req"]
    assert req.target_type == NotifyTargetTypeEnum.CUSTOM
    assert req.target_value == "12345"  # 仅该新用户可见
    assert req.publish_now is True
    assert "爱丽丝" in req.title
    assert "12345" in req.content


def test_push_new_user_notify_failure_swallowed():
    """失败路径：服务层抛异常时不向上抛（弱依赖），返回 False。"""
    with (
        patch("app.mq.rpc_pptr_user.new_session", return_value=_fake_session_ctx()),
        patch(
            "app.mq.rpc_pptr_user.NotifyService.create_idempotent",
            new=AsyncMock(side_effect=RuntimeError("db down")),
        ),
    ):
        ok = asyncio.run(_push_new_user_notify(user_name="bob", uid=2))

    assert ok is False


def test_rpc_create_user_push_only_when_created():
    """仅新建用户时发布欢迎通知；已存在用户（upsert）不发布。"""
    params = PptrCreateUserParams(uid=0, user_name="alice", pwd="")

    # created=False：老用户重复登录 / Casdoor 同步，不应再发欢迎通知
    with (
        patch(
            "app.mq.rpc_pptr_user.PptrUserService.create_user",
            new=AsyncMock(return_value=(12345, False)),
        ),
        patch("app.mq.rpc_pptr_user._push_new_user_notify", new=AsyncMock()) as mock_push,
    ):
        asyncio.run(rpc_create_user(params))
    mock_push.assert_not_awaited()

    # created=True：新用户，发布欢迎通知
    with (
        patch(
            "app.mq.rpc_pptr_user.PptrUserService.create_user",
            new=AsyncMock(return_value=(12345, True)),
        ),
        patch("app.mq.rpc_pptr_user._push_new_user_notify", new=AsyncMock()) as mock_push,
    ):
        resp = asyncio.run(rpc_create_user(params))
    mock_push.assert_awaited_once()
    assert mock_push.await_args.kwargs["uid"] == 12345
    assert resp.code == 0  # 弱依赖：即使推送失败也不影响 RPC 成功返回
