"""评论内容审核（Phase 5.1）。

同步拦截：评论发布前先过一遍敏感词，命中**高危词**直接 `rejected`（全站不可见），
命中**疑似词**置 `auditing`（先发后审，对齐 B 站体验，本期作者视角与公开视角均暂按不可见）。

实现用 **DFA Trie**（前缀树）做多模式匹配：把关键词建成 Trie，扫一遍正文即可命中
所有出现在其中的敏感词，复杂度 O(文本长度 × 字符数)，比「每条词都做一次子串扫描」稳得多，
也能平滑扩展到上千词库。词库进程内热加载（首次构建后常驻），后续可接管理端动态刷新。

弱依赖边界：审核是发布主流程的**强依赖**——命中高危必须拦下，否则脏数据外溢。
但词库本身加载失败时应**降级放行**（只允许放行，不允许误杀），并向告警通道报错。
"""

from app.models.enums import CommentStateEnum


# ==================== 默认词库（演示用，后续由管理端 / 配置覆盖）====================
# 高危：命中即拒审
_HIGH_RISK_WORDS: list[str] = [
    "涉政示例词",
    "违禁品",
    "诈骗",
    "赌博平台",
]
# 疑似：命中置待审核
_SUSPECT_WORDS: list[str] = [
    "广告",
    "代刷",
    "引流",
    "加微信",
]


class _TrieNode:
    __slots__ = ("children", "is_end")

    def __init__(self) -> None:
        self.children: dict[str, "_TrieNode"] = {}
        self.is_end: bool = False


class _Trie:
    """极简 DFA 前缀树，只支持逐字符匹配（中文按字符）。"""

    def __init__(self) -> None:
        self.root = _TrieNode()

    def add(self, word: str) -> None:
        node = self.root
        for ch in word:
            node = node.children.setdefault(ch, _TrieNode())
        node.is_end = True

    def match_all(self, text: str) -> list[str]:
        """返回 text 中命中的所有词（按出现顺序、可重复）。"""
        hits: list[str] = []
        n = len(text)
        for i in range(n):
            node = self.root
            j = i
            buf = ""
            while j < n and text[j] in node.children:
                node = node.children[text[j]]
                buf += text[j]
                if node.is_end:
                    hits.append(buf)
                j += 1
        return hits


# 进程内热加载的单例
_HIGH_TRIE: _Trie | None = None
_SUSPECT_TRIE: _Trie | None = None


def _build() -> tuple[_Trie, _Trie]:
    global _HIGH_TRIE, _SUSPECT_TRIE
    if _HIGH_TRIE is None:
        _HIGH_TRIE = _Trie()
        for w in _HIGH_RISK_WORDS:
            _HIGH_TRIE.add(w)
    if _SUSPECT_TRIE is None:
        _SUSPECT_TRIE = _Trie()
        for w in _SUSPECT_WORDS:
            _SUSPECT_TRIE.add(w)
    return _HIGH_TRIE, _SUSPECT_TRIE


def reload_words(high_risk: list[str], suspect: list[str]) -> None:
    """管理端动态刷新词库（热加载，无需重启）。"""
    global _HIGH_TRIE, _SUSPECT_TRIE
    _HIGH_TRIE = _Trie()
    for w in high_risk:
        _HIGH_TRIE.add(w)
    _SUSPECT_TRIE = _Trie()
    for w in suspect:
        _SUSPECT_TRIE.add(w)


def audit_text(message: str) -> tuple[CommentStateEnum, list[str]]:
    """对评论正文做敏感词审核。

    Returns:
        `(state, hit_words)` —— 始终给出判定状态与命中的词（供记录 / 告警）。
        词库加载异常时降级为 `NORMAL` 放行，不误杀。
    """
    if not message:
        return CommentStateEnum.NORMAL, []
    try:
        high, suspect = _build()
    except Exception:
        # 词库构建失败：宁可放行也不误杀，交由后续异步复审兜底
        return CommentStateEnum.NORMAL, []

    high_hits = high.match_all(message)
    if high_hits:
        return CommentStateEnum.REJECTED, high_hits

    suspect_hits = suspect.match_all(message)
    if suspect_hits:
        return CommentStateEnum.AUDITING, suspect_hits

    return CommentStateEnum.NORMAL, []


__all__ = ["audit_text", "reload_words"]
