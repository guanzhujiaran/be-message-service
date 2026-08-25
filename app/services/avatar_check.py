"""头像图片 URL 下载校验（P13，2.16.0）。

修改头像只接受**图片 URL 链接**（不接收文件上传）。后端主动下载该 URL 做校验：

1. **URL 路径后缀名白名单**（2.28.1 新增）：`urllib.parse.urlparse` 提取路径（忽略 query），
   取路径最后一段的后缀并小写，必须命中 `{".jpg",".jpeg",".png",".gif",".webp",".bmp",".avif"}`；
   无后缀 / 非图片后缀（如 `.php`/`.exe`/`.svg`/`.txt`）一律拒绝——下载前拦截，不产生网络请求，
   防止提交指向可执行文件、脚本或任意非图片资源的 URL（SSRF 面收敛 + 内容类型混淆攻击面减小）；
2. **1s 内下载完成**：`httpx.AsyncClient` 超时 1s，超时 / 网络错误即拒绝；
3. **文件大小 ≤ 1MB**：流式读取累计字节，超限立即断连拒绝（防止大文件 / 慢速源拖垮接口）。

附加约束：
- 仅允许 `http` / `https` 协议（拒绝 `file://` 等）；
- 响应 `Content-Type` 必须为图片（`image/*`）；
- 仅读取头部少量字节即可判断 Content-Type 与大小，不整图驻留内存。

校验通过后由调用方（`/user_info/update` 接口）复用 `PptrUserService.set_user_detail(face=...)`
写入 pptr `TUserDetail.avatar`。
"""

import httpx
from loguru import logger
from urllib.parse import urlparse

# 头像大小上限：1MB
AVATAR_MAX_BYTES = 1024 * 1024
# 下载超时：1s（确保「1s 内下载完成」）
AVATAR_DOWNLOAD_TIMEOUT = 1.0
# 流式读取块大小（64KB）
_READ_CHUNK = 64 * 1024
# URL 路径后缀名白名单（2.28.1 防攻击加固）：SVG 含脚本执行能力故排除
ALLOWED_IMAGE_EXTENSIONS = frozenset(
    {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".avif"}
)


def _path_extension(url: str) -> str:
    """提取 URL 路径的文件后缀（小写，含点，如 ".jpg"）。

    用 `urlparse` 仅取路径部分（忽略 query/fragment），再取路径最后一段的后缀；
    无后缀（如 `https://x.com/img`）返回空字符串。用于 2.28.1 后缀名白名单校验。
    """
    path = urlparse(url).path
    name = path.rsplit("/", 1)[-1]
    if "." not in name:
        return ""
    return "." + name.rsplit(".", 1)[-1].lower()


async def verify_avatar_url(
    url: str, *, transport: httpx.AsyncBaseTransport | None = None, label: str = "头像"
) -> tuple[bool, str]:
    """校验图片 URL 是否合法可用（头像 / 收藏夹封面等场景共用，2.28.0 起支持自定义 label）。

    Args:
        url: 图片 URL 链接。
        transport: 可选 httpx transport（测试注入 MockTransport 用；生产默认 None 走真实网络）。
        label: 校验对象的业务名称（默认"头像"，用于错误文案；收藏夹封面传"封面"）。

    Returns:
        (ok, reason)：ok=True 校验通过；ok=False 时 reason 为失败原因文案（供接口 422 返回）。
    """
    if not url:
        return False, f"{label}链接不能为空"
    if not url.lower().startswith(("http://", "https://")):
        return False, f"{label}链接仅支持 http/https"

    # 2.28.1 防攻击加固：URL 路径后缀名白名单（下载前拦截，不产生网络请求）
    ext = _path_extension(url)
    if ext not in ALLOWED_IMAGE_EXTENSIONS:
        allowed = "/".join(sorted(ALLOWED_IMAGE_EXTENSIONS, key=len))
        logger.warning(f"{label}链接后缀非法被拒: {url}")
        return False, f"{label}链接必须是图片文件（支持 {allowed} 后缀）"

    timeout = httpx.Timeout(AVATAR_DOWNLOAD_TIMEOUT)
    try:
        async with httpx.AsyncClient(
            timeout=timeout, follow_redirects=True, transport=transport
        ) as client:
            async with client.stream("GET", url) as resp:
                if resp.status_code != 200:
                    return False, f"{label}图片下载失败（HTTP {resp.status_code}）"
                content_type = resp.headers.get("content-type", "")
                if not content_type.startswith("image/"):
                    return False, f"{label}链接不是有效的图片"
                # 流式读取累计大小，超 1MB 立即断连拒绝
                total = 0
                async for chunk in resp.aiter_bytes(chunk_size=_READ_CHUNK):
                    total += len(chunk)
                    if total > AVATAR_MAX_BYTES:
                        logger.warning(f"{label}图片超过 1MB 被拒: {url}")
                        return False, f"{label}图片不能超过 1MB"
    except httpx.TimeoutException:
        logger.warning(f"{label}图片下载超时（>{AVATAR_DOWNLOAD_TIMEOUT}s）: {url}")
        return False, f"{label}图片下载超时或网络异常"
    except httpx.HTTPError as e:
        logger.warning(f"{label}图片下载网络异常: {url} -> {e}")
        return False, f"{label}图片下载超时或网络异常"

    return True, ""


__all__ = [
    "AVATAR_MAX_BYTES",
    "AVATAR_DOWNLOAD_TIMEOUT",
    "ALLOWED_IMAGE_EXTENSIONS",
    "verify_avatar_url",
]
