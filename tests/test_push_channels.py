"""推送渠道连通性测试。

读取全局配置 ``settings.message_config``，对所有「已配置凭据（token）」的渠道
逐一发起一次真实推送，报告成功 / 失败，便于排查哪些渠道的 token 还有效。
渠道失败时抛出的 RuntimeError 已包含 ``url`` / ``body`` / 响应，可直接定位问题。

运行（在 be-message-service 目录下）::

    uv run pytest tests/test_push_channels.py -v

注意：本测试会真实发送消息，请确保已配置预期的渠道凭据。
"""

import asyncio

import pytest

from app.core.config import settings
from app.services.push import PushMessageService


def _collect_enabled_channels() -> list[str]:
    """收集当前配置下已启用（即配置了 token / 凭据）的渠道。"""
    svc = PushMessageService(settings.message_config)
    return [m for m in svc.get_available_methods() if svc._is_enabled(m)]


ENABLED_CHANNELS = _collect_enabled_channels()


def test_has_enabled_channels():
    """至少应存在一个配置了 token 的渠道，否则本测试无意义。"""
    if not ENABLED_CHANNELS:
        pytest.skip(
            "未检测到任何已配置 token 的推送渠道，"
            "请先在 MESSAGE_CONFIG / .env 中设置至少一个渠道凭据"
        )


@pytest.mark.parametrize("channel", ENABLED_CHANNELS)
async def test_channel_push(channel: str):
    """对每个已配置 token 的渠道发起一次真实推送。"""
    svc = PushMessageService(settings.message_config)
    method = getattr(svc, channel)
    title = "【连通性测试】message-service"
    content = "这是一条来自自动化测试的消息，用于验证推送渠道 token 是否有效。"
    try:
        if asyncio.iscoroutinefunction(method):
            await method(title, content)
        else:
            await asyncio.get_event_loop().run_in_executor(None, method, title, content)
    except Exception as e:  # noqa: BLE001
        pytest.fail(f"渠道 {channel} 推送失败：{e}")
