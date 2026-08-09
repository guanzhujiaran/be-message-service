"""系统通知正文的轻量标记语言。

格式与 B 站 `feedsystem/system_notify` 的内联链接保持一致：

    #{显示文本}{"https://example.com"}

只支持一种元素：「文本 + URL 链接」。理由：

- 需求来源就是 B 站那条「评论存在违规」通知——正文里既有视频 BV 号锚点、
  也有「《社区公约》」外链、「查看详情」按钮型外链。
- 我们后台现在能拿到的就是「来源评论区」和一段原文摘要，没有更复杂的结构需求。
- 不引入 Markdown / HTML，避免 XSS、转义、第三方依赖——前端只需一个简单的
  正则解析与 `` 渲染。

本模块刻意做成纯字符串构造，方便在 ``f""`` 里和业务代码穿插使用：

    lines = [
        f"您在{markup_inline_link(source.label, source.url)}下发布的评论被举报...",
        f"原文：{excerpt}",
    ]

URL 必须以 ``http://`` / ``https://`` 开头，否则视为非法，由 ``is_safe_url()``
兜底拒绝，防止有人构造 ``javascript:...`` / ``data:...`` 的伪链接绕过。
"""

from __future__ import annotations

__all__ = ["markup_inline_link", "INLINE_LINK_RE"]


# 形如 #{文本}{"url"} —— 注意 url 部分是双引号包裹的字面量，便于正则区分「链接结束」与文本里出现的右花括号
# URL 既支持 https:// 外链，也支持 /app/... 站内路径，方便直接复用 build_comment_source() 的产出
INLINE_LINK_RE = r'#\{([^{}]*?)\}\{"((?:https?://[^"\s]+|/app/\S+))"\}'


def _is_safe_target(url: str) -> bool:
    """只允许 ``http(s)://`` 外链与 ``/app/...`` 站内相对路径，过滤 ``javascript:`` / ``data:`` 等伪协议。

    站内路径只接受 ``/app/`` 前缀——这是当前前端约定；其他相对路径（一级目录、动态拼接等）
    暂不放行，避免让用户写的通知能跳到任意站内路由增加被滥用面。
    """
    if not url:
        return False
    lowered = url.strip().lower()
    if lowered.startswith("http://") or lowered.startswith("https://"):
        return True
    return lowered.startswith("/app/")


def markup_inline_link(text: str, url: str | None) -> str:
    """生成一段 ``#{text}{"url"}`` 内联链接片段。

    当 ``url`` 为空、不安全或 ``text`` 为空时，直接返回纯文本 ``text``，
    不渲染为链接——避免出现「链接指向空」这种半残废状态。
    """
    if not url or not text:
        return text
    if not _is_safe_target(url):
        return text
    return f'#{{{text}}}{{"{url}"}}'