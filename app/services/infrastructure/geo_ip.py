"""GeoIP 属地解析服务。

基于 GeoLite2 mmdb 数据库（MaxMind），把客户端 IP 解析为：
- 城市 / 省份名称（供动态 lbsPoi 展示，形如「浙江 杭州」）
- 经纬度（lbsLat / lbsLng）

数据库文件（GeoLite2-City.mmdb / GeoLite2-Country.mmdb / GeoLite2-ASN.mmdb）：
- 本地开发：放在 settings.geoip_mmdb_dir（默认 ./mmdb）
- Docker：由 docker-compose volume 挂载
- 下载脚本：scripts/download_geoip_mmdb.py（三个文件均来自 gitee 镜像仓库）

设计：
- **懒加载单例**：首次查询时按需打开 mmdb 文件，Reader 线程安全可复用；
- **静默降级**：mmdb 缺失 / IP 为内网 / 解析失败时返回 None，不影响动态发布主流程。
"""

from __future__ import annotations
import geoip2.database

import ipaddress
from functools import lru_cache
from pathlib import Path
from typing import Any

from loguru import logger

from app.core.config import settings

_CITY_DB = "GeoLite2-City.mmdb"
_ASN_DB = "GeoLite2-ASN.mmdb"


class GeoIpResult:
    """一次 IP 解析结果（属地 + 经纬度 + ISP）。"""

    __slots__ = ("poi", "lat", "lng", "isp")

    def __init__(
        self,
        poi: str,
        lat: float | None = None,
        lng: float | None = None,
        isp: str | None = None,
    ) -> None:
        self.poi = poi
        self.lat = lat
        self.lng = lng
        self.isp = isp


@lru_cache(maxsize=1)
def _city_reader() -> Any | None:
    """打开 City 库（懒加载 + 进程级缓存）。失败返回 None。"""
    path = Path(settings.geoip_mmdb_dir) / _CITY_DB
    if not path.is_file():
        logger.warning(f"GeoIP City 库不存在: {path}，跳过属地解析")
        return None
    return geoip2.database.Reader(str(path), locales=["zh-CN"])


@lru_cache(maxsize=1)
def _asn_reader() -> Any | None:
    """打开 ASN 库（ISP 运营商解析，懒加载）。失败返回 None。"""
    path = Path(settings.geoip_mmdb_dir) / _ASN_DB
    if not path.is_file():
        return None
    return geoip2.database.Reader(str(path))


def _is_private_or_loopback(ip: str) -> bool:
    """内网 / 回环 / 保留地址不解析（无属地意义，且 mmdb 大概率没有）。"""
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return True
    return addr.is_private or addr.is_loopback or addr.is_link_local or addr.is_reserved


def _lookup_isp(ip: str) -> str | None:
    """按 ASN 库解析运营商（ISP）名称；无 ASN 库 / 未命中返回 None。"""
    reader = _asn_reader()
    if reader is None:
        return None
    try:
        resp = reader.asn(ip)
    except Exception:  # noqa: BLE001 - ASN 未命中常见，静默跳过
        return None
    org = (resp.autonomous_system_organization or "").strip()
    return org or None


def lookup(ip: str | None) -> GeoIpResult:
    """按 IP 解析属地（国家/城市 + 经纬度 + ISP）。

    任何解析失败场景（IP 为空 / 内网 / mmdb 缺失 / 未命中）都用「未知」兜底，
    返回带 ``poi="未知"`` 的 ``GeoIpResult``（lat/lng/isp 为 None），不抛异常。
    这样上游（动态/评论发布）总能拿到一个可保存的属地，前端显示「未知」。
    """
    if not ip:
        return GeoIpResult(poi="未知")
    if _is_private_or_loopback(ip):
        return GeoIpResult(poi="未知")
    reader = _city_reader()
    if reader is None:
        return GeoIpResult(poi="未知")
    try:
        resp = reader.city(ip)
    except Exception:  # noqa: BLE001 - mmdb 未命中（保留地址等）常见，静默跳过
        return GeoIpResult(poi="未知")

    city = resp.city.names.get("zh-CN") or resp.city.name
    province = (
        resp.subdivisions.most_specific.names.get("zh-CN")
        if resp.subdivisions.most_specific
        else None
    )
    lat = resp.location.latitude
    lng = resp.location.longitude
    if lat is None or lng is None:
        return GeoIpResult(poi="未知")

    parts = [p for p in (province, city) if p]
    poi = " ".join(parts) if parts else (resp.country.names.get("zh-CN") or "未知")
    return GeoIpResult(
        poi=poi, lat=float(lat), lng=float(lng), isp=_lookup_isp(ip)
    )


def lookup_poi(ip: str | None) -> str | None:
    """仅取属地名称（动态 lbsPoi 用）。解析失败返回「未知」。"""
    return lookup(ip).poi


__all__ = ["lookup", "lookup_poi", "GeoIpResult"]
