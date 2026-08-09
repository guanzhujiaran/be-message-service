"""通用工具函数。"""

from app.utils.ip_mask import (
    extract_client_ip,
    mask_ip_pair,
    mask_ipv4,
    mask_ipv6,
)

__all__ = [
    "extract_client_ip",
    "mask_ipv4",
    "mask_ipv6",
    "mask_ip_pair",
]
