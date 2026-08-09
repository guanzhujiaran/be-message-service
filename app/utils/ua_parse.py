"""轻量级 User-Agent 解析。

不引入额外依赖，仅根据常见 UA 关键字提取平台（plat）与设备（device），
用于评论 / 私信等内容发布时记录来源。
"""

from __future__ import annotations

import re


_MOBILE_BRANDS = (
    "xiaomi",
    "redmi",
    "samsung",
    "oppo",
    "vivo",
    "huawei",
    "honor",
    "oneplus",
    "realme",
    "meizu",
    "htc",
    "sony",
    "nokia",
    "lg",
)


def parse_user_agent(ua: str | None) -> tuple[str | None, str | None]:
    """从 User-Agent 字符串解析 plat 与 device。

    Returns:
        (plat, device)，无法识别时返回 None。
    """
    if not ua:
        return None, None

    lowered = ua.lower()

    # ---- plat：操作系统 / 平台 ----
    if "android" in lowered:
        plat = "android"
    elif "iphone" in lowered or "ipad" in lowered or "ipod" in lowered or "ios" in lowered:
        plat = "ios"
    elif "harmonyos" in lowered or "harmony" in lowered:
        plat = "harmonyos"
    elif "windows" in lowered:
        plat = "windows"
    elif "macintosh" in lowered or "mac os" in lowered:
        plat = "macos"
    elif "linux" in lowered:
        plat = "linux"
    else:
        plat = None

    # ---- device：设备类型 / 型号 ----
    device: str | None = None
    if "ipad" in lowered:
        device = "ipad"
    elif "iphone" in lowered:
        # 尽量取具体型号，如 iPhone15,2 -> iPhone
        device = "iphone"
    elif "android" in lowered:
        for brand in _MOBILE_BRANDS:
            if brand in lowered:
                device = brand
                break
        if device is None:
            # 尝试匹配 Build/ 前的设备名，如 "Mi 10 Build/..."
            match = re.search(r";\s*([^;]+?)\s+build/", lowered)
            if match:
                device = match.group(1).strip() or "android"
            else:
                device = "android"
    elif "windows" in lowered:
        device = "pc"
    elif "macintosh" in lowered or "mac os" in lowered:
        device = "pc"
    elif "linux" in lowered:
        device = "pc"

    return plat, device
