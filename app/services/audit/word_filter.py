"""通用敏感词过滤（DFA / Trie，三层词库：high / medium / low）。

设计要点
--------
1. **词库即数据**：词表以静态文件放在 `data/sensitive-words/{high,medium,low}.txt`
   （一行一词，`#` 开头为注释，空行忽略）。升级词库 = 替换词表文件，无需改代码。
   计划采用 https://github.com/fwwdn/sensitive-stop-words 的内容填充。
2. **三层风险分级**：`high`（命中即拒绝）/ `medium`（进人工）/ `low`（仅记录不阻断）。
3. **DFA Trie 多模式匹配**：扫一遍文本命中全部词，复杂度 O(文本长度 × 字符)，
   比逐词子串扫描稳，可平滑扩展到上千词。
4. **弱依赖边界**：词库加载失败 → 降级为空词库（宁可放行，不误杀），并可热加载重建。
5. **防误杀三道闸**（词库是外部导入的，掺杂通用词会大面积误杀）：
   - 分隔符**按文件类型区分**（对齐上游 sensitive-stop-words 官方格式）：
     逗号分隔文件（`色情类.txt` / `政治类.txt`）按 `,` 切分，其余文件一行一词；
     **不把 `|` `／` `、` 当分隔符**（它们不是词库分隔符，误切会产出「出售」「专卖」
     等通用碎片词），也**不按空白切分**（`燃烧弹 制作` 不再切出「制作」），
     带空格短语额外补一条去空白的紧凑形式；
   - 长度 < `MIN_WORD_LEN`（单字，如「枪」「卖」）的词不参与匹配；
   - `whitelist.txt` 里的通用词整词相等时豁免（长词照常命中）。

本模块抽自 `services/comment/comment_audit.py` 的 DFA 实现，全系统复用。
"""

from __future__ import annotations

import os
import re
from enum import IntEnum
from threading import Lock

# 项目根（be-message-service/），词表路径从此处派生
_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
#: 词库目录（可通过环境变量覆盖，便于测试 / 部署自定义词库）
WORDS_DIR = os.environ.get("SENSITIVE_WORDS_DIR") or os.path.join(
    _PROJECT_ROOT, "data", "sensitive-words"
)

_HIGH_FILE = "high.txt"
_MEDIUM_FILE = "medium.txt"
_LOW_FILE = "low.txt"

#: 误杀豁免词（白名单）：一行一词，与词表同格式。
#: 词库里残留的通用词（如「制作」「方法」「电话」）单独命中不具判别力，
#: 放进白名单后**整词相等**才豁免（长词仍按其本身规则生效）。
WHITELIST_FILE = "whitelist.txt"
#: 参与匹配的最小词长（单字词几乎无判别力，「卖」「枪」等会大面积误杀，直接丢弃）
MIN_WORD_LEN = 2

#: 分类词库目录：每个 `.txt` 为一个**词库自带分类**（分类名 = 文件名去扩展名）。
#: 采用 sensitive-stop-words 等词库时，直接按仓库自带分类把词放进对应文件即可，
#: 命中理由可直接用分类名（如「广告类」「涉政类」）告知用户。
CATEGORIES_DIRNAME = "categories"
#: 分类 → 风险层级映射（`levels.json`，形如 `{"广告类": "medium", "涉政类": "high"}`）；
#: 未列出的分类默认 MEDIUM（安全优先，只进人工不误杀）。
LEVELS_FILE = "levels.json"


def _effective_words_dir() -> str:
    """词库目录：环境变量 `SENSITIVE_WORDS_DIR` 优先，否则默认 WORDS_DIR。

    便于测试 / 部署临时指向自定义词库（无需改代码）。
    """
    return os.environ.get("SENSITIVE_WORDS_DIR") or WORDS_DIR


def categories_dir() -> str:
    """分类词库子目录（词库根 / `categories/`），可用 env 覆盖。

    sensitive-stop-words 按「仓库自带分类」组织（如 `政治类.txt` / `色情类.txt`）。
    分类词库可放在 `词库根/categories/` 子目录，也可直接放词库根目录——
    引擎加载时会**同时扫描词库根与 `categories/` 子目录**（子目录优先），
    命中同一分类以子目录为准。
    """
    env_dir = os.environ.get("SENSITIVE_WORDS_CATEGORIES_DIR")
    if env_dir:
        return env_dir
    return os.path.join(_effective_words_dir(), CATEGORIES_DIRNAME)


#: 词库根目录里「非文本敏感词分类」的保留名（域名规则 / 映射配置 / 停止词不作文本敏感词）
_RESERVED_FILENAMES = {
    LEVELS_FILE,  # levels.json
    WHITELIST_FILE,  # 误杀豁免词，非敏感词
    "domains_blacklist.txt",
    "domains_whitelist.txt",
    "网址.txt",  # 域名黑名单数据（交由 link_checker 处理，不作文本敏感词）
    "stopword.dic",  # 停止词 / 分词用，非内容风险词，不作文本敏感词
    "high.txt",
    "medium.txt",
    "low.txt",
}


#: 上游词库中「逗号分隔」的文件（其余文件均为「一行一词」）。
#: 见 https://github.com/fwwdn/sensitive-stop-words —— 官方仅 色情类 / 政治类 用逗号分隔。
_COMMA_DELIMITED_FILES = frozenset({"色情类.txt", "政治类.txt"})


def _is_comma_delimited(path: str) -> bool:
    """该词表文件是否按逗号分隔（只认文件名，与所在目录无关）。"""
    return os.path.basename(path) in _COMMA_DELIMITED_FILES


def _split_line(line: str, *, comma_delimited: bool = False) -> list[str]:
    """清洗一行词（分隔符按文件类型区分，对齐 sensitive-stop-words 官方格式）。

    上游词库只区分两种格式（见 README「词库概览」）：
    - **逗号分隔**（`色情类.txt` / `政治类.txt`）：一行可含多词，按 `,` 切分；
    - **一行一词**（`广告.txt` / `涉枪涉爆违法信息关键词.txt` / `stopword.dic` 等）：
      整行即一个词。

    **不把 `|` `／` `、` 当分隔符**：它们并非词库分隔符，误切会产出「出售」「专卖」
    等通用碎片词（如 `猎枪出售/枪` → `猎枪出售`+`枪`、`气枪/出售/专卖网` → 含「出售」）。
    **也不按空白切分**：词库里大量条目是「燃烧弹 制作」「TNT 炸弹的制作」这类
    带空格的短语，按空格切开会产出「制作」「方法」「电话」等通用碎片词，
    单独命中即误杀（如「感谢某某 制作」被判涉枪涉爆高危）。
    对带空格的短语额外补一条「去空白的紧凑形式」，兼顾 `原子弹制作方法` 的写法。
    """
    chunks = re.split(r"[,，]+", line) if comma_delimited else [line]
    parts: list[str] = []
    for chunk in chunks:
        w = chunk.strip()
        if not w:
            continue
        # 去除成对/零星引号等纯标点残余
        w = w.strip("\"'“”‘’·.")
        if not w:
            continue
        parts.append(w)
        compact = re.sub(r"\s+", "", w)
        if compact != w and len(compact) >= MIN_WORD_LEN:
            parts.append(compact)
    return parts


def load_whitelist() -> set[str]:
    """加载误杀豁免词（`whitelist.txt`），不存在 / 读取失败返回空集合。"""
    return set(_load_words_from_path(os.path.join(_effective_words_dir(), WHITELIST_FILE)))


def _usable_word(word: str, whitelist: set[str]) -> bool:
    """词条是否参与匹配：过短（单字）或在白名单里的通用词一律丢弃。"""
    if len(word) < MIN_WORD_LEN:
        return False
    return word not in whitelist


def load_category_levels() -> dict[str, str]:
    """加载 `levels.json`（分类 → 层级名），不存在 / 解析失败返回空字典。"""
    path = os.path.join(_effective_words_dir(), LEVELS_FILE)
    if not os.path.isfile(path):
        return {}
    try:
        import json

        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return {str(k): str(v) for k, v in data.items()}
    except Exception:  # noqa: BLE001
        return {}
    return {}


def _level_from_name(name: str) -> "WordLevel":
    """层级名 → WordLevel（未知 / 缺省按 MEDIUM，避免误杀）。"""
    key = (name or "").strip().lower()
    if key in ("high", "h", "3", "reject", "拒绝"):
        return WordLevel.HIGH
    if key in ("low", "l", "1", "pass", "记录"):
        return WordLevel.LOW
    return WordLevel.MEDIUM


class WordLevel(IntEnum):
    """敏感词风险层级（决定命中后的审核判定）。"""

    HIGH = 3  # 命中即自动拒绝
    MEDIUM = 2  # 命中进人工审核
    LOW = 1  # 命中仅记录，不阻断


_LEVEL_FILE = {
    WordLevel.HIGH: _HIGH_FILE,
    WordLevel.MEDIUM: _MEDIUM_FILE,
    WordLevel.LOW: _LOW_FILE,
}


class TrieNode:
    __slots__ = ("children", "is_end")

    def __init__(self) -> None:
        self.children: dict[str, TrieNode] = {}
        self.is_end: bool = False


class Trie:
    """极简 DFA 前缀树，逐字符匹配（中文按字符）。"""

    def __init__(self) -> None:
        self.root = TrieNode()

    def add(self, word: str) -> None:
        if not word:
            return
        node = self.root
        for ch in word:
            node = node.children.setdefault(ch, TrieNode())
        node.is_end = True

    def match_all(self, text: str) -> list[str]:
        """返回 text 中命中的所有词（按出现顺序，可重复）。"""
        hits: list[str] = []
        if not text:
            return hits
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


def _load_words_from_path(path: str) -> list[str]:
    """从指定路径加载词（忽略注释行；分隔符按文件类型区分）。

    逗号分隔文件（`色情类.txt` / `政治类.txt`）按 `,` 切分，其余文件一行一词，
    对齐 sensitive-stop-words 官方格式（见 `_split_line`）。
    """
    if not os.path.isfile(path):
        return []
    comma_delimited = _is_comma_delimited(path)
    words: list[str] = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                stripped = line.strip()
                if not stripped or stripped.startswith("#"):
                    continue
                words.extend(_split_line(stripped, comma_delimited=comma_delimited))
    except Exception:  # noqa: BLE001
        return []
    return words


def load_words_from_file(filename: str) -> list[str]:
    """从词库目录下的词表文件加载词。

    文件不存在或读取异常 → 返回空列表（弱依赖降级，不阻断）。
    """
    return _load_words_from_path(os.path.join(_effective_words_dir(), filename))


class WordFilter:
    """三层敏感词过滤器（进程内单例，支持热加载）。"""

    def __init__(self) -> None:
        self._tries: dict[WordLevel, Trie] = {lv: Trie() for lv in WordLevel}
        #: 分类词库：{分类名: (层级, Trie)}——分类名直接作为命中理由告知用户
        self._categories: dict[str, tuple[WordLevel, Trie]] = {}
        self._lock = Lock()
        self._loaded = False

    # ---------------- 加载 / 热加载 ----------------

    def _load_categories_locked(self) -> None:
        """加载分类词库（文件名为分类名，层级由 levels.json 决定）。

        扫描分类目录：优先 `categories/` 子目录，否则词库根目录下的
        `.txt` / `.dic` 分类文件；排除保留名（域名规则 / levels.json / 网址域名等）。
        """
        self._categories = {}
        levels = load_category_levels()
        whitelist = load_whitelist()
        # 候选目录：categories/ 子目录（若存在） + 词库根目录
        dirs: list[str] = []
        sub = categories_dir()
        root = _effective_words_dir()
        for d in (sub, root):
            if os.path.isdir(d) and d not in dirs:
                dirs.append(d)
        seen_categories: set[str] = set()
        for d in dirs:
            try:
                names = sorted(
                    f
                    for f in os.listdir(d)
                    if f.lower().endswith((".txt", ".dic"))
                    and os.path.isfile(os.path.join(d, f))
                    and f not in _RESERVED_FILENAMES
                )
            except Exception:  # noqa: BLE001
                continue
            for filename in names:
                category = os.path.splitext(filename)[0]
                if category in seen_categories:
                    continue  # 同分类以首次出现为准（categories/ 优先于根）
                seen_categories.add(category)
                level = _level_from_name(levels.get(category, "medium"))
                trie = Trie()
                words = [
                    w for w in _load_words_from_path(os.path.join(d, filename))
                    if _usable_word(w, whitelist)
                ]
                for w in words:
                    trie.add(w)
                if words:
                    self._categories[category] = (level, trie)

    def load(self, *, force: bool = False) -> None:
        """从词表目录加载三层词库 + 分类词库（首次调用自动触发；force=True 重建）。"""
        with self._lock:
            if self._loaded and not force:
                return
            whitelist = load_whitelist()
            for level in WordLevel:
                trie = Trie()
                for w in load_words_from_file(_LEVEL_FILE[level]):
                    if _usable_word(w, whitelist):
                        trie.add(w)
                self._tries[level] = trie
            self._load_categories_locked()
            self._loaded = True

    def reload_words(
        self,
        high: list[str] | None = None,
        medium: list[str] | None = None,
        low: list[str] | None = None,
    ) -> None:
        """管理端动态刷新词库（热加载，无需重启）。

        传入某层为 None 时不改动该层（保留当前已加载内容）。
        同样过滤单字词与白名单词，与管理端 / 文件词库口径一致。
        """
        with self._lock:
            whitelist = load_whitelist()
            provided = {
                WordLevel.HIGH: high,
                WordLevel.MEDIUM: medium,
                WordLevel.LOW: low,
            }
            for level, words in provided.items():
                if words is None:
                    continue
                trie = Trie()
                for w in words:
                    if _usable_word(w, whitelist):
                        trie.add(w)
                self._tries[level] = trie
            self._loaded = True

    # ---------------- 匹配 ----------------

    def match_categories(self, text: str) -> dict[str, tuple[WordLevel, list[str]]]:
        """按**词库自带分类**匹配，返回 `{分类名: (层级, 命中词列表)}`（仅含命中的分类）。

        分类名可直接作为「命中理由」告知用户（如「广告类」「涉政类」）。
        """
        if not text:
            return {}
        try:
            self.load()
        except Exception:  # noqa: BLE001
            return {}
        out: dict[str, tuple[WordLevel, list[str]]] = {}
        for category, (level, trie) in self._categories.items():
            hits = trie.match_all(text)
            if hits:
                out[category] = (level, hits)
        return out

    def match(self, text: str) -> dict[WordLevel, list[str]]:
        """对文本做三层匹配（三层词库 + 分类词库按层级归并），返回 `{层级: 命中词列表}`。"""
        if not text:
            return {}
        try:
            self.load()
        except Exception:  # noqa: BLE001
            # 词库构建 / 加载失败：降级为空匹配，宁可放行不误杀
            return {}
        out: dict[WordLevel, list[str]] = {}
        for level in (WordLevel.HIGH, WordLevel.MEDIUM, WordLevel.LOW):
            hits = self._tries[level].match_all(text)
            if hits:
                out[level] = hits
        # 分类词库按层级归并进三层结果（去重且保持顺序）
        for _category, (level, hits) in self.match_categories(text).items():
            merged = out.setdefault(level, [])
            for w in hits:
                if w not in merged:
                    merged.append(w)
        return out

    @property
    def loaded(self) -> bool:
        return self._loaded


# 进程内单例
word_filter = WordFilter()

__all__ = [
    "WordLevel",
    "Trie",
    "TrieNode",
    "WordFilter",
    "word_filter",
    "load_words_from_file",
    "load_category_levels",
    "load_whitelist",
    "WHITELIST_FILE",
    "MIN_WORD_LEN",
    "categories_dir",
    "WORDS_DIR",
]
