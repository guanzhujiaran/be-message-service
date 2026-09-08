"""正文链接 / 域名风控。

从文字内容中提取 http(s) 链接，按域名黑 / 白名单判定：

- **黑名单命中**（广告 / 钓鱼 / 外链诱导等）→ 拒绝；
- **白名单命中**（站内、可信图片域名等）→ 放行；
- **无法判定**（既不在黑也不在白，且白名单非空）→ 进人工；
- 无链接 → 直接放行。

规则同样「数据化」：黑 / 白名单各为一个词表文件，与敏感词库同目录
（`data/sensitive-words/domains_blacklist.txt` / `domains_whitelist.txt`），
一行一条域名（支持子域：`example.com` 命中 `a.example.com`），`#` 开头为注释。
白名单为空 = 不限制所有非黑名单域名（默认宽松，避免误杀）。
"""

from __future__ import annotations

import os
import re
from urllib.parse import urlparse

from app.services.audit.word_filter import _effective_words_dir

_BLACKLIST_FILES = ("网址.txt", "domains_blacklist.txt")
_WHITELIST_FILE = "domains_whitelist.txt"

_URL_RE = re.compile(r"https?://[^\s，,。;；)）\"'<>]+", re.IGNORECASE)


def _load_domains(filename: str) -> list[str]:
    path = os.path.join(_effective_words_dir(), filename)
    if not os.path.isfile(path):
        return []
    out: list[str] = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                d = line.strip().lower()
                if not d or d.startswith("#"):
                    continue
                out.append(d)
    except Exception:  # noqa: BLE001
        return []
    return out


def _load_blacklist() -> list[str]:
    """加载域名黑名单：优先 fwwdn 自带的 `网址.txt`，否则 `domains_blacklist.txt`。"""
    for fn in _BLACKLIST_FILES:
        path = os.path.join(_effective_words_dir(), fn)
        if os.path.isfile(path):
            return _load_domains(fn)
    return []


def extract_urls(text: str) -> list[str]:
    """提取文本中的 http(s) 链接。"""
    if not text:
        return []
    return _URL_RE.findall(text)


def _host_of(url: str) -> str:
    try:
        host = urlparse(url).hostname or ""
    except Exception:  # noqa: BLE001
        return ""
    return host.lower()


def _domain_matches(host: str, domain: str) -> bool:
    """域名匹配：相等或为其子域。"""
    return host == domain or host.endswith("." + domain)


class LinkCheckResult:
    """链接检查结果。"""

    def __init__(
        self,
        urls: list[str],
        blocked: list[str],
        allowed: list[str],
        unknown: list[str],
    ) -> None:
        self.urls = urls
        #: 命中黑名单的域名
        self.blocked = blocked
        #: 命中白名单的域名
        self.allowed = allowed
        #: 未命中任何名单的域名（白名单为空时恒为空 → 视为放行）
        self.unknown = unknown

    @property
    def has_url(self) -> bool:
        return bool(self.urls)


def check_links(text: str) -> LinkCheckResult:
    """对文本中的链接做域名黑 / 白名单检查。"""
    urls = extract_urls(text)
    if not urls:
        return LinkCheckResult([], [], [], [])

    blacklist = _load_blacklist()
    whitelist = _load_domains(_WHITELIST_FILE)

    blocked: list[str] = []
    allowed: list[str] = []
    unknown: list[str] = []
    seen: set[str] = set()
    for url in urls:
        host = _host_of(url)
        if not host or host in seen:
            continue
        seen.add(host)
        if any(_domain_matches(host, d) for d in blacklist):
            blocked.append(host)
        elif any(_domain_matches(host, d) for d in whitelist):
            allowed.append(host)
        elif whitelist:
            # 白名单非空才有「未知域名」概念（严格模式）
            unknown.append(host)
    return LinkCheckResult(urls, blocked, allowed, unknown)


__all__ = ["LinkCheckResult", "check_links", "extract_urls"]
