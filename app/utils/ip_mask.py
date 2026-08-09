"""客户端 IP 提取与脱敏。

产品约定（与「属地解析」方案的区别）：

- **只存原始 IPv4 / IPv6，不做属地转换**：不引入 ip2region 等离线库，
  省掉一份几十 MB 的数据文件与随之而来的版本维护。
- **出参一律打码**：普通用户只能看到粗粒度前缀，明文仅管理员接口返回。
- **前端优先展示 IPv6**：v6 覆盖率更能代表真实接入网络；无 v6 时回落 v4。

打码粒度的取舍：保留的位数足以区分「大致运营商 / 大区」，
又不足以定位到具体用户，兼顾展示价值与隐私保护。

    IPv4  203.0.113.45            → 203.0.*.*
    IPv6  2408:8207:78d2:1a00::1  → 2408:8207:*
"""

import ipaddress

# 请求头优先级：网关注入的可信头 > 反代链路头 > 直连 socket 地址。
# `x-bili-ip` 由 be-gateway 网关写入（若已配置），可信度最高。
_IP_HEADERS: tuple[str, ...] = (
    "x-bili-ip",
    "x-forwarded-for",
    "x-real-ip",
)


def _normalize(raw: str | None) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """把字符串解析成 ipaddress 对象，非法值返回 None。

    IPv4-mapped IPv6（`::ffff:203.0.113.45`）会被还原成 IPv4，
    否则同一个客户端会因为接入链路不同被记成两种协议。
    """
    if not raw:
        return None
    candidate = raw.strip()
    if not candidate:
        return None
    # 形如 [2408:8207::1]:54321 / 203.0.113.45:54321 的带端口写法
    if candidate.startswith("["):
        candidate = candidate[1:].split("]", 1)[0]
    elif candidate.count(":") == 1:
        candidate = candidate.split(":", 1)[0]

    try:
        addr = ipaddress.ip_address(candidate)
    except ValueError:
        return None

    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        return addr.ipv4_mapped
    return addr


def extract_client_ip(headers: dict[str, str], peer: str | None = None) -> tuple[str | None, str | None]:
    """从请求头 + socket 地址中提取客户端 IP。

    Args:
        headers: 请求头（键名大小写不敏感，调用方传入已小写化的字典亦可）。
        peer: `request.client.host`，所有头都拿不到时的兜底。

    Returns:
        `(ipv4, ipv6)`：同一次请求通常只会命中其中一个，另一个为 None。

    Note:
        若网关未透传真实客户端 IP，这里拿到的会是网关的内网地址，
        此时字段没有展示价值 —— 需要在 nginx / 网关侧确认透传链路。
    """
    lowered = {str(k).lower(): v for k, v in headers.items()}

    for name in _IP_HEADERS:
        value = lowered.get(name)
        if not value:
            continue
        # X-Forwarded-For 是 "client, proxy1, proxy2" 形式，首段才是真实客户端
        first = str(value).split(",")[0]
        addr = _normalize(first)
        if addr is not None:
            break
    else:
        addr = _normalize(peer)

    if addr is None:
        return None, None
    if isinstance(addr, ipaddress.IPv4Address):
        return str(addr), None
    return None, str(addr)


def mask_ipv4(ip: str | None) -> str | None:
    """IPv4 打码：保留前两段。`203.0.113.45` → `203.0.*.*`"""
    if not ip:
        return None
    parts = ip.split(".")
    if len(parts) != 4:
        return None
    return f"{parts[0]}.{parts[1]}.*.*"


def mask_ipv6(ip: str | None) -> str | None:
    """IPv6 打码：保留前两组。`2408:8207:78d2:1a00::1` → `2408:8207:*`"""
    if not ip:
        return None
    try:
        # 先展开成完整形式，避免 `::` 压缩导致取到的前两组语义不一致
        exploded = ipaddress.IPv6Address(ip).exploded
    except ValueError:
        return None
    groups = exploded.split(":")
    if len(groups) < 2:
        return None
    # 去掉前导零，贴近用户熟悉的缩写写法
    head = [g.lstrip("0") or "0" for g in groups[:2]]
    return f"{head[0]}:{head[1]}:*"


def mask_ip_pair(ip_v4: str | None, ip_v6: str | None) -> tuple[str | None, str | None]:
    """一次性把 (v4, v6) 打码，供出参组装调用。"""
    return mask_ipv4(ip_v4), mask_ipv6(ip_v6)


__all__ = [
    "extract_client_ip",
    "mask_ipv4",
    "mask_ipv6",
    "mask_ip_pair",
]
