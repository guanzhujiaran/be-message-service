"""统一内容审核服务（敏感词 + 用户风控 + 链接/域名规则 + 结果通知）。

本包承载全系统文字内容的**自动审核**能力（自动优先，尽量少进人工）：

- `word_filter`：DFA / Trie 三层敏感词 + **词库自带分类**词库（`categories/`），词表文件热加载；
- `link_checker`：正文链接提取 + 域名黑 / 白名单；
- `engine`：统一审核引擎 `audit_text()`，输出 `PASS / AUDIT / REJECT` 判定，
  并把「分类 → 命中词」带入结果（分类名直接作为告知用户的理由，词已打码）；
- `mask`：敏感词打码（仅对外展示 / 通知用，库内命中词仍存原文）；
- `notify`：审核未通过 / 进人工时给作者发站内系统通知（正文脱敏）。

调用方（各资源发布入口）只需：
    from app.services.audit import audit_text, send_audit_notice
    result = audit_text(content, risk=user_risk)
    if result.rejected:  # 拒绝：落库原文 + 通知作者 + 拦截发布
        ...
    elif result.need_manual:  # 进人工
        ...
"""

from app.services.audit.engine import (
    AuditDecision,
    AuditResult,
    UserRisk,
    audit_text,
)
from app.services.audit.link_checker import LinkCheckResult, check_links, extract_urls
from app.services.audit.mask import mask_hits_by_category, mask_text, mask_word
from app.services.audit.notify import DEFAULT_REJECT_TITLE, send_audit_notice
from app.services.audit.word_filter import (
    WHITELIST_FILE,
    WORDS_DIR,
    WordFilter,
    WordLevel,
    load_whitelist,
    word_filter,
)

__all__ = [
    "audit_text",
    "AuditDecision",
    "AuditResult",
    "UserRisk",
    "check_links",
    "extract_urls",
    "LinkCheckResult",
    "mask_word",
    "mask_text",
    "mask_hits_by_category",
    "send_audit_notice",
    "DEFAULT_REJECT_TITLE",
    "word_filter",
    "WordFilter",
    "WordLevel",
    "WORDS_DIR",
    "load_whitelist",
    "WHITELIST_FILE",
]
