"""敏感词脱敏（打码）。

用途：给用户的通知 / 界面文案里需要展示命中内容时，先把敏感词打码
（避免把完整敏感词再次展示给用户），但**数据库落库的命中词仍为原文**
（供审核 / 审计查看），脱敏只在「对外展示 / 通知」环节调用。

打码策略（保留少量首尾便于用户理解，又不泄露完整敏感词）：
- 1 字 → `*`
- 2 字 → 首字 + `*`（如 `诈*`）
- ≥3 字 → 首字 + 中间全 `*` + 尾字（如 `赌*台`）
"""

from __future__ import annotations


def mask_word(word: str) -> str:
    """把单个敏感词打码。"""
    n = len(word)
    if n <= 0:
        return word
    if n == 1:
        return "*"
    if n == 2:
        return word[0] + "*"
    return word[0] + "*" * (n - 2) + word[-1]


def mask_text(text: str, words: list[str]) -> str:
    """把 text 中出现的所有 `words` 命中词打码，返回脱敏文本。

    Args:
        text: 原文（含未打码的敏感词）。
        words: 命中的敏感词列表（原文）。

    Returns:
        打码后的文本（不修改原文本 / 不落库）。
    """
    if not text or not words:
        return text
    # 长词优先替换，避免短词先替换破坏长词的原文匹配
    masked = text
    for word in sorted(set(words), key=len, reverse=True):
        if not word:
            continue
        masked = masked.replace(word, mask_word(word))
    return masked


def mask_hits_by_category(
    text: str, hits: dict[str, list[str]]
) -> dict[str, list[str]]:
    """把「分类 → 命中词」里的命中词在 text 中打码，返回 `{分类: [打码词]}`。"""
    return {cat: [mask_word(w) for w in wl] for cat, wl in hits.items()}


__all__ = ["mask_word", "mask_text", "mask_hits_by_category"]
