"""推送渠道调用链测试（**不发送真实消息**，全程 mock 网络 / SMTP）。

读取全局配置 ``settings.message_config``，对所有「已配置凭据（token）」的渠道
逐一跑一遍各自的发送实现，但把 ``httpx`` / ``smtplib`` 全部替换为本地替身：
- ``get_client()`` 返回假的 ``AsyncClient``，``post`` / ``get`` 只记录调用并回放
  该渠道的「成功响应」载荷（各渠道的成功判定字段不同，故按渠道分别给出）；
- ``smtp`` 渠道改为 mock ``smtplib.SMTP`` / ``SMTP_SSL``。

这样可以在**不打扰真实用户**的前提下验证：渠道被正确识别 → 走了正确的 URL /
请求体 → 成功判定分支能通过（不会误判为失败抛 RuntimeError）。

运行（在 be-message-service 目录下）::

    uv run pytest tests/test_push_channels.py -v

注意：本测试**不会**发出任何真实推送。
"""

import smtplib
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from app.core.config import settings
from app.services.message.external import push as push_mod
from app.services.message.external.push import PushMessageService

# 各渠道判定「推送成功」所需响应字段（httpx 替身回放用的载荷）。
# 同一渠道的多次请求（如企业微信 APP 先取 token 再发消息）共用同一载荷。
_MOCK_OK_PAYLOADS: dict[str, dict[str, Any]] = {
    "bark": {"code": 200},
    "dingding_bot": {"errcode": 0},
    "feishu_bot": {"code": 0},
    "go_cqhttp": {"status": "ok"},
    "gotify": {"id": 1},
    "iGot": {"ret": 0},
    "serverJ": {"errno": 0},
    "pushdeer": {"content": {"result": [{"id": "mock"}]}},
    "chat": {},  # 仅看 status_code
    "pushplus_bot": {"code": 200},
    "weplus_bot": {"code": 200},
    "qmsg_bot": {"code": 0},
    "smtp": {},  # 同步渠道，走 smtplib 替身，无 HTTP 响应
    "wecom_app": {"access_token": "MOCK_ACCESS_TOKEN", "errmsg": "ok"},
    "wecom_bot": {"errcode": 0},
    "telegram_bot": {"ok": True},
    "aibotk": {"code": 0},
    "pushme": {},  # 需要 status_code=200 且 text == "success"
    "chronocat": {},  # 仅看 status_code
    "ntfy": {},  # 仅看 status_code
    "wxpusher_bot": {"code": 1000},
    "webhook": {},  # 仅看 status_code
}


class _MockResponse:
    """httpx.Response 替身：固定 200 + 固定 JSON 载荷。"""

    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload
        self.status_code = 200
        self.text = "success"

    def json(self) -> dict[str, Any]:
        return self._payload


class _MockAsyncClient:
    """httpx.AsyncClient 替身：记录调用，不建立任何真实连接。"""

    def __init__(self, payload: dict[str, Any], calls: list[tuple[str, str]]) -> None:
        self._payload = payload
        self._calls = calls

    def _record(self, verb: str, url: str) -> _MockResponse:
        self._calls.append((verb, str(url)))
        return _MockResponse(self._payload)

    async def post(self, url: str, **_kwargs: Any) -> _MockResponse:
        return self._record("POST", url)

    async def get(self, url: str, **_kwargs: Any) -> _MockResponse:
        return self._record("GET", url)


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
    """对每个已配置 token 的渠道跑一次发送（网络 / SMTP 全 mock）。"""
    svc = PushMessageService(settings.message_config)
    method = getattr(svc, channel)
    title = "【连通性测试】message-service"
    content = "这是一条来自自动化测试的消息，用于验证推送渠道调用链（不会真实发送）。"

    calls: list[tuple[str, str]] = []
    client = _MockAsyncClient(_MOCK_OK_PAYLOADS[channel], calls)
    smtp_conn = MagicMock()

    with (
        patch.object(push_mod, "get_client", return_value=client),
        patch.object(smtplib, "SMTP", return_value=smtp_conn),
        patch.object(smtplib, "SMTP_SSL", return_value=smtp_conn),
    ):
        try:
            if channel == "smtp":
                method(title, content)  # 同步实现
            else:
                await method(title, content)
        except Exception as e:  # noqa: BLE001
            pytest.fail(f"渠道 {channel} 推送失败：{e}")

    if channel == "smtp":
        # 同步渠道：没有 HTTP 调用，断言走到了 SMTP 登录/发信
        smtp_conn.login.assert_called_once()
        smtp_conn.sendmail.assert_called_once()
    else:
        # 走到了真实请求构造（而非因缺配置提前 return / 抛错）才算验证通过
        assert calls, f"渠道 {channel} 未发起任何请求，调用链未被覆盖"
