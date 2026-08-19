"""头像图片 URL 下载校验单元测试（P13-T5，2.16.0）。

用 `httpx.MockTransport` 注入假响应，覆盖：
- 非法协议拒绝（file://）
- 非图片 Content-Type 拒绝
- 文件大小 > 1MB 拒绝
- 下载超时 / 网络异常拒绝
- 正常小图通过
"""

import httpx
import pytest

from app.services.avatar_check import AVATAR_MAX_BYTES, verify_avatar_url


def _mock_transport(content: bytes | None = None, content_type: str = "image/jpeg", status_code: int = 200):
    """构造返回固定内容的 MockTransport。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code=status_code, content=content or b"", headers={"content-type": content_type})

    return httpx.MockTransport(handler)


@pytest.mark.anyio
async def test_reject_non_http_protocol():
    ok, reason = await verify_avatar_url("file:///etc/passwd")
    assert not ok
    assert "http/https" in reason


@pytest.mark.anyio
async def test_reject_empty_url():
    ok, reason = await verify_avatar_url("")
    assert not ok
    assert "不能为空" in reason


@pytest.mark.anyio
async def test_reject_non_image_content_type():
    transport = _mock_transport(content=b"hello", content_type="text/html")
    ok, reason = await verify_avatar_url("https://img.example.com/x", transport=transport)
    assert not ok
    assert "不是有效的图片" in reason


@pytest.mark.anyio
async def test_reject_http_error_status():
    transport = _mock_transport(content_type="image/jpeg", status_code=404)
    ok, reason = await verify_avatar_url("https://img.example.com/x", transport=transport)
    assert not ok
    assert "下载失败" in reason


@pytest.mark.anyio
async def test_reject_oversize_image():
    # 超过 1MB 的图片被拒
    big = b"0" * (AVATAR_MAX_BYTES + 1)
    transport = _mock_transport(content=big, content_type="image/jpeg")
    ok, reason = await verify_avatar_url("https://img.example.com/big.jpg", transport=transport)
    assert not ok
    assert "1MB" in reason


@pytest.mark.anyio
async def test_reject_network_timeout():
    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("connect timed out", request=request)

    transport = httpx.MockTransport(handler)
    ok, reason = await verify_avatar_url("https://img.example.com/slow.jpg", transport=transport)
    assert not ok
    assert "超时或网络异常" in reason


@pytest.mark.anyio
async def test_accept_valid_small_image():
    transport = _mock_transport(content=b"\xff\xd8\xff\xe0binary", content_type="image/jpeg")
    ok, reason = await verify_avatar_url("https://img.example.com/ok.jpg", transport=transport)
    assert ok is True
    assert reason == ""
