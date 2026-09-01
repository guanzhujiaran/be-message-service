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

URL 支持三种形态（见计划书 §2.10，正则与前端 ``src/utils/notifyContent.ts`` 必须同步）：

- ``http://`` / ``https://`` 外链；
- ``route:{路由名}?{query}`` 站内跳转（**现行写法**，路由名取自
  :class:`app.models.enums.FrontendRouteEnum`，由 :func:`app.utils.route_target.build_route_target` 生成）；
- ``/app/...`` 站内路径（**仅存量数据兼容**，新代码一律用路由名）。

非法目标（``javascript:`` / ``data:`` 等伪协议、未注册的路由名）由 ``_is_safe_target()``
兜底拒绝，防止有人构造伪链接绕过。
"""

from __future__ import annotations

from app.utils.route_target import is_route_target

__all__ = ["INLINE_LINK_RE", "markup_inline_link"]


# 形如 #{文本}{"url"} —— 注意 url 部分是双引号包裹的字面量，便于正则区分「链接结束」与文本里出现的右花括号
# url 三选一：https:// 外链 | route:站内路由名（现行） | /app/... 站内路径（存量兼容）
INLINE_LINK_RE = r'#\{([^{}]*?)\}\{"((?:https?://[^"\s]+|/app/\S+|route:[^"\s]+))"\}'


def _is_safe_target(url: str) -> bool:
    """只放行三类目标：``http(s)://`` 外链、``route:`` + 已注册路由名、``/app/...`` 站内路径。

    过滤 ``javascript:`` / ``data:`` 等伪协议；``route:`` 形态还要求路由名已在
    :class:`FrontendRouteEnum` 注册（见 ``app/utils/route_target.py``），
    避免让用户写的通知能跳到任意站内位置增加被滥用面。
    """
    if not url:
        return False
    stripped = url.strip()
    lowered = stripped.lower()
    if lowered.startswith("http://") or lowered.startswith("https://"):
        return True
    if lowered.startswith("route:"):
        return is_route_target(stripped)
    # 存量数据兼容：历史通知里落过 /app/... 站内路径，只接受 /app/ 前缀
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