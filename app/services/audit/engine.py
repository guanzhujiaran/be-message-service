"""统一内容审核引擎（敏感词 + 用户风控 + 链接/域名，自动优先）。

产出三种判定（调用方据此分流）：
- `PASS`   —— 自动通过，直接放行（无需人工）；
- `AUDIT`  —— 进人工审核队列（仅疑似，尽量少）；
- `REJECT` —— 自动拒绝（明确违规）。

判定顺序（先严后宽，命中即短路）：
1. 链接域名黑名单 → REJECT；
2. 敏感词 high → REJECT（用户风控从严时 medium 也升为 REJECT）；
3. 敏感词 medium → AUDIT（从严时也仍为 REJECT，见上）；
4. 链接未知域名（白名单非空时）→ AUDIT；
5. 敏感词 low → 仅记录（判定仍为 PASS）；
6. 其余 → PASS。

用户风控（`UserRisk`）决定「是否从严」：高风险用户（封禁中 / 违规累计多 / 低等级新号）
发布的内容升一级处置，把风险挡在自动层。引擎不直连用户表——由调用方按需填充，
保证引擎可单测、可复用。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum

from app.services.audit.link_checker import LinkCheckResult, check_links
from app.services.audit.word_filter import WordLevel, word_filter


class AuditDecision(IntEnum):
    """审核判定：通过 / 进人工 / 拒绝。"""

    PASS = 1
    AUDIT = 2
    REJECT = 3


@dataclass
class UserRisk:
    """用户风控画像（由调用方填充，默认无风险）。

    Args:
        is_banned: 用户是否处于封禁中（be-message msg_user_ban / RPA rpa_user_ban）。
        violation_count: 历史违规累计次数。
        is_low_level: 是否低等级 / 新号（发言门槛维度）。
    """

    is_banned: bool = False
    violation_count: int = 0
    is_low_level: bool = False

    @property
    def strict(self) -> bool:
        """是否对该用户从严（命中规则升一级）。"""
        return self.is_banned or self.violation_count >= 3 or self.is_low_level


@dataclass
class AuditResult:
    """审核结果。"""

    decision: AuditDecision
    #: 命中的敏感词（按风险层级归类，**原文**，供落库 / 审计）
    hit_words: dict[WordLevel, list[str]] = field(default_factory=dict)
    #: 命中的敏感词（按**词库自带分类**归类，`{分类名: 命中词原文}`）
    hit_categories: dict[str, list[str]] = field(default_factory=dict)
    #: 链接检查结果
    links: LinkCheckResult | None = None
    #: 用户风控画像
    risk: UserRisk | None = None
    #: 判定原因（供流水 / 通知展示）
    reason: str = ""

    @property
    def passed(self) -> bool:
        return self.decision == AuditDecision.PASS

    @property
    def rejected(self) -> bool:
        return self.decision == AuditDecision.REJECT

    @property
    def need_manual(self) -> bool:
        return self.decision == AuditDecision.AUDIT

    @property
    def all_hit_words(self) -> list[str]:
        """全部命中词**原文**（跨层级去重）。

        仅供对外展示前脱敏（`mask_text(excerpt, result.all_hit_words)`）使用，
        不要直接下发给用户——对外文案一律走打码。
        """
        out: list[str] = []
        for words in self.hit_words.values():
            for w in words:
                if w not in out:
                    out.append(w)
        return out


def audit_text(
    text: str,
    *,
    risk: UserRisk | None = None,
    check_link: bool = True,
) -> AuditResult:
    """对一段文字内容做统一审核。

    Args:
        text: 待审文本（各资源的文字字段：正文 / 名称 / 描述 / 理由等）。
        risk: 用户风控画像（None = 无风险）。
        check_link: 是否启用链接 / 域名规则（纯文本场景可关）。

    Returns:
        AuditResult（含判定、命中词、链接结果、原因）。
    """
    risk = risk or UserRisk()
    hit_words = word_filter.match(text) if text else {}
    # match_categories 返回 {分类: (层级, 命中词)}；这里只取命中词（层级已并入 hit_words/决策）
    raw_categories = word_filter.match_categories(text) if text else {}
    hit_categories: dict[str, list[str]] = {
        cat: hits for cat, (_level, hits) in raw_categories.items()
    }
    links = check_links(text) if (check_link and text) else None

    # ① 链接黑名单：明确违规
    if links is not None and links.blocked:
        return AuditResult(
            decision=AuditDecision.REJECT,
            hit_words=hit_words,
            hit_categories=hit_categories,
            links=links,
            risk=risk,
            reason=f"包含违规链接域名：{', '.join(links.blocked)}",
        )

    # ② 高危敏感词：明确违规
    high_hits = hit_words.get(WordLevel.HIGH) or []
    if high_hits:
        return AuditResult(
            decision=AuditDecision.REJECT,
            hit_words=hit_words,
            hit_categories=hit_categories,
            links=links,
            risk=risk,
            reason=f"命中高危敏感词（{_hits_reason(hit_words, hit_categories)}）",
        )

    # ③ 中风险敏感词：通常进人工；用户风控从严时直接拒
    medium_hits = hit_words.get(WordLevel.MEDIUM) or []
    if medium_hits:
        if risk.strict:
            return AuditResult(
                decision=AuditDecision.REJECT,
                hit_words=hit_words,
                hit_categories=hit_categories,
                links=links,
                risk=risk,
                reason=f"高风险用户命中敏感词（{_hits_reason(hit_words, hit_categories)}）",
            )
        return AuditResult(
            decision=AuditDecision.AUDIT,
            hit_words=hit_words,
            hit_categories=hit_categories,
            links=links,
            risk=risk,
            reason=f"命中疑似敏感词（{_hits_reason(hit_words, hit_categories)}），待人工复核",
        )

    # ④ 链接未知域名（白名单非空的严格模式）：进人工
    if links is not None and links.unknown:
        return AuditResult(
            decision=AuditDecision.AUDIT,
            hit_words=hit_words,
            hit_categories=hit_categories,
            links=links,
            risk=risk,
            reason=f"包含无法判定的链接域名：{', '.join(links.unknown)}",
        )

    # ⑤ 低风险敏感词：仅记录，判定仍为通过
    low_hits = hit_words.get(WordLevel.LOW) or []
    if low_hits and risk.strict:
        return AuditResult(
            decision=AuditDecision.AUDIT,
            hit_words=hit_words,
            hit_categories=hit_categories,
            links=links,
            risk=risk,
            reason=f"高风险用户命中低风险词（{_hits_reason(hit_words, hit_categories)}）",
        )

    return AuditResult(
        decision=AuditDecision.PASS,
        hit_words=hit_words,
        hit_categories=hit_categories,
        links=links,
        risk=risk,
        reason="自动审核通过" if not low_hits else f"通过（记录低风险词：{_hits_reason(hit_words, hit_categories)}）",
    )


def _hits_reason(
    hit_words: dict[WordLevel, list[str]],
    hit_categories: dict[str, list[str]],
) -> str:
    """把命中词压缩为供通知 / 理由展示的简短文本（词一律打码，不泄露原文）。

    优先按「词库自带分类」展示（分类名即理由）；无分类（三层词库命中）时，
    按层级列出打码词。
    """
    if not hit_words:
        return ""
    from app.services.audit.mask import mask_word

    # ① 有分类：用分类名 + 打码词
    if hit_categories:
        parts = []
        for cat, words in hit_categories.items():
            masked = "、".join(sorted({mask_word(w) for w in _compact_hits(words)}))
            parts.append(f"{cat}：{masked}")
        return "；".join(parts)
    # ② 无分类：按层级列出打码词
    label = {WordLevel.HIGH: "高危", WordLevel.MEDIUM: "疑似", WordLevel.LOW: "低风险"}
    parts = []
    for level in (WordLevel.HIGH, WordLevel.MEDIUM, WordLevel.LOW):
        words = hit_words.get(level) or []
        if words:
            masked = "、".join(sorted({mask_word(w) for w in _compact_hits(words)}))
            parts.append(f"{label[level]}词：{masked}")
    return "；".join(parts)


def _compact_hits(words: list[str]) -> list[str]:
    """展示前压缩命中词：被更长命中词包含的短词不再重复列出。

    例如同时命中「炸药」「出售炸药」时只展示「出售炸药」，
    避免理由里出现一串互相包含的碎片（落库仍保留全部命中词，不丢审计信息）。
    """
    kept: list[str] = []
    for w in sorted(set(words), key=len, reverse=True):
        if any(w != k and w in k for k in kept):
            continue
        kept.append(w)
    return kept


__all__ = [
    "AuditDecision",
    "AuditResult",
    "UserRisk",
    "audit_text",
]
