"""头像图片 URL 下载校验（P13，2.16.0）。

修改头像只接受**图片 URL 链接**（不接收文件上传）。后端主动下载该 URL 做两道校验：

1. **1s 内下载完成**：`httpx.AsyncClient` 超时 1s，超时 / 网络错误即拒绝；
2. **文件大小 ≤ 1MB**：流式读取累计字节，超限立即断连拒绝（防止大文件 / 慢速源拖垮接口）。

附加约束：
- 仅允许 `http` / `https` 协议（拒绝 `file://` 等）；
- 响应 `Content-Type` 必须为图片（`image/*`）；
- 仅读取头部少量字节即可判断 Content-Type 与大小，不整图驻留内存。

校验通过后由调用方（`/user_info/update` 接口）复用 `PptrUserService.set_user_detail(face=...)`
写入 pptr `TUserDetail.avatar`。
"""

import httpx
from loguru import logger

# 头像大小上限：1MB
AVATAR_MAX_BYTES = 1024 * 1024
# 下载超时：1s（确保「1s 内下载完成」）
AVATAR_DOWNLOAD_TIMEOUT = 1.0
# 流式读取块大小（64KB）
_READ_CHUNK = 64 * 1024


async def verify_avatar_url(
    url: str, *, transport: httpx.AsyncBaseTransport | None = None
) -> tuple[bool, str]:
    """校验头像图片 URL 是否合法可用。

    Args:
        url: 图片 URL 链接。
        transport: 可选 httpx transport（测试注入 MockTransport 用；生产默认 None 走真实网络）。

    Returns:
        (ok, reason)：ok=True 校验通过；ok=False 时 reason 为失败原因文案（供接口 422 返回）。
    """
    if not url:
        return False, "头像链接不能为空"
    if not url.lower().startswith(("http://", "https://")):
        return False, "头像链接仅支持 http/https"

    timeout = httpx.Timeout(AVATAR_DOWNLOAD_TIMEOUT)
    try:
        async with httpx.AsyncClient(
            timeout=timeout, follow_redirects=True, transport=transport
        ) as client:
            async with client.stream("GET", url) as resp:
                if resp.status_code != 200:
                    return False, f"头像图片下载失败（HTTP {resp.status_code}）"
                content_type = resp.headers.get("content-type", "")
                if not content_type.startswith("image/"):
                    return False, "头像链接不是有效的图片"
                # 流式读取累计大小，超 1MB 立即断连拒绝
                total = 0
                async for chunk in resp.aiter_bytes(chunk_size=_READ_CHUNK):
                    total += len(chunk)
                    if total > AVATAR_MAX_BYTES:
                        logger.warning(f"头像图片超过 1MB 被拒: {url}")
                        return False, "头像图片不能超过 1MB"
    except httpx.TimeoutException:
        logger.warning(f"头像图片下载超时（>{AVATAR_DOWNLOAD_TIMEOUT}s）: {url}")
        return False, "头像图片下载超时或网络异常"
    except httpx.HTTPError as e:
        logger.warning(f"头像图片下载网络异常: {url} -> {e}")
        return False, "头像图片下载超时或网络异常"

    return True, ""


__all__ = [
    "AVATAR_MAX_BYTES",
    "AVATAR_DOWNLOAD_TIMEOUT",
    "verify_avatar_url",
]
