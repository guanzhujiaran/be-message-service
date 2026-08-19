"""下载 GeoLite2 mmdb 数据库到本地（供 GeoIP 属地解析使用）。

数据库文件来自 gitee 镜像仓库（https://gitee.com/sbf123456/GeoLite.mmdb）：
- GeoLite2-ASN.mmdb
- GeoLite2-City.mmdb
- GeoLite2-Country.mmdb

用法：
    # 全部下载到默认目录（be-message-service/mmdb，本地开发用）
    uv run python scripts/download_geoip_mmdb.py

    # 指定下载目录（如 Docker 挂载点 docker_vol/geoip/mmdb）
    uv run python scripts/download_geoip_mmdb.py --dir /path/to/mmdb

说明：
- 文件较大（City 库约几十 MB），下载完成后会打印校验结果；
- 动态发布时若 mmdb 缺失会静默降级（位置字段为空），不影响主流程。
"""

from __future__ import annotations

import argparse
import base64
import sys
from pathlib import Path

import httpx
from loguru import logger

# gitee 镜像仓库：文件 blob sha（来自仓库默认分支 download）
# https://gitee.com/sbf123456/GeoLite.mmdb
_GITEE_REPO = "sbf123456/GeoLite.mmdb"
_DB_BLOBS: dict[str, str] = {
    "GeoLite2-ASN.mmdb": "67b73d71c280ab2065a5ec9c83de7b3577c5ab98",
    "GeoLite2-City.mmdb": "9801db51ea51bc9f03f4d3a54ce347f9e8ce323c",
    "GeoLite2-Country.mmdb": "7a5e67a559d0f3631b1192070de42dc8d9b90444",
}

# 各库预期体积（字节，用于完整性校验）
_EXPECTED_SIZE = {
    "GeoLite2-ASN.mmdb": 12_035_428,
    "GeoLite2-City.mmdb": 65_590_693,
    "GeoLite2-Country.mmdb": 8_689_303,
}


def download_one(client: httpx.Client, name: str, sha: str, out: Path) -> None:
    """通过 gitee git blob API 下载单个 mmdb（base64），失败则删除半成品。"""
    dest = out / name
    logger.info(f"下载 {name} …")
    url = f"https://gitee.com/api/v5/repos/{_GITEE_REPO}/git/blobs/{sha}"
    try:
        resp = client.get(url, timeout=300)
        resp.raise_for_status()
        data = resp.json()
        if data.get("encoding") != "base64":
            raise RuntimeError(f"意外响应 encoding: {data.get('encoding')}")
        raw = "".join(data["content"].split())
        dest.write_bytes(base64.b64decode(raw))
    except Exception as e:  # noqa: BLE001
        dest.unlink(missing_ok=True)
        raise RuntimeError(f"{name} 下载失败: {e}") from e

    size = dest.stat().st_size
    expected = _EXPECTED_SIZE.get(name)
    if expected is not None and size != expected:
        dest.unlink(missing_ok=True)
        raise RuntimeError(f"{name} 下载不完整（{size} 字节 ≠ 预期 {expected}），已删除")
    logger.success(f"  {name}: {size / 1024 / 1024:.1f} MB OK")


def main() -> None:
    p = argparse.ArgumentParser(description="下载 GeoLite2 mmdb 数据库")
    p.add_argument(
        "--dir",
        default="mmdb",
        help="下载目录（默认 ./mmdb 本地开发用；Docker 环境请用 --dir ../docker_vol/geoip/mmdb）",
    )
    args = p.parse_args()

    out = Path(args.dir)
    out.mkdir(parents=True, exist_ok=True)

    with httpx.Client() as client:
        ok = True
        for name, sha in _DB_BLOBS.items():
            try:
                download_one(client, name, sha, out)
            except RuntimeError as e:
                logger.error(str(e))
                ok = False

    if ok:
        logger.success(f"全部数据库下载完成，目录: {out.resolve()}")
    else:
        logger.error("部分数据库下载失败，请检查网络后重试")
        sys.exit(1)


if __name__ == "__main__":
    main()
