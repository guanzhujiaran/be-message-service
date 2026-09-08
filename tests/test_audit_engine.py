"""统一内容审核引擎单元测试（敏感词 + 用户风控 + 链接/域名，自动优先）。

覆盖：
- 干净文本自动通过（PASS）；
- 高危词自动拒绝（REJECT）；
- 中等词进人工（AUDIT）；
- 用户风控从严：中等词升拒绝、低风险词升人工；
- 链接域名黑名单拒绝 / 白名单放行 / 未知域名进人工（白名单非空时）；
- 词库自带分类命中（levels.json 映射层级）；敏感词打码；reason 脱敏。
"""

import os

import pytest

from app.services.audit.engine import (
    AuditDecision,
    UserRisk,
    audit_text,
)
from app.services.audit.word_filter import WordLevel, word_filter

# 词库目录 / 分类目录名（与模块常量一致，测试内字符串避免包遮蔽）
_CAT_DIRNAME = "categories"
_LEVELS_FILENAME = "levels.json"


@pytest.fixture(autouse=True)
def _default_words():
    """每个用例前用测试词库（明确可控），结束后恢复文件词库。"""
    word_filter.reload_words(high=["诈骗"], medium=["广告"], low=["口水词"])
    yield
    word_filter.load(force=True)


@pytest.fixture
def words_dir(tmp_path, monkeypatch):
    """把词库目录临时指向 tmp_path，并创建 categories 子目录。"""
    cat = tmp_path / _CAT_DIRNAME
    cat.mkdir()
    monkeypatch.setenv("SENSITIVE_WORDS_DIR", str(tmp_path))
    return tmp_path


def test_clean_text_passes():
    result = audit_text("今天天气真好")
    assert result.passed
    assert result.decision == AuditDecision.PASS
    assert not result.hit_words


def test_empty_text_passes():
    result = audit_text("")
    assert result.passed


def test_high_risk_word_rejected():
    result = audit_text("这是一个诈骗网站")
    assert result.rejected
    assert result.decision == AuditDecision.REJECT
    # 库内命中词存原文（供审核 / 审计）
    assert result.hit_words[WordLevel.HIGH] == ["诈骗"]
    # 对外 reason 已打码，不含原文
    assert "诈*" in result.reason
    assert "诈骗" not in result.reason


def test_medium_word_needs_manual():
    result = audit_text("欢迎来看我的广告")
    assert result.need_manual
    assert result.decision == AuditDecision.AUDIT
    assert "广告" in result.hit_words[WordLevel.MEDIUM]


def test_strict_user_medium_becomes_reject():
    """用户风控从严：中等敏感词升级为自动拒绝（少进人工）。"""
    result = audit_text("欢迎来看我的广告", risk=UserRisk(violation_count=5))
    assert result.rejected
    assert result.risk is not None and result.risk.strict


def test_strict_user_low_word_becomes_manual():
    """用户风控从严：低风险词也进人工。"""
    result = audit_text("口水词", risk=UserRisk(is_banned=True))
    assert result.need_manual


def test_low_word_only_recorded_for_normal_user():
    """普通用户命中低风险词：仅记录，判定仍通过。"""
    result = audit_text("口水词")
    assert result.passed
    assert "口水词" in result.hit_words[WordLevel.LOW]


def test_reload_words_takes_effect():
    """热加载后新词立即生效（无需重启）。"""
    word_filter.reload_words(high=["新高危词"])
    result = audit_text("这里有新高危词")
    assert result.rejected
    word_filter.reload_words(high=["诈骗"], medium=["广告"], low=["口水词"])
    assert audit_text("这里有新高危词").passed


# ---------------- 链接 / 域名 ----------------

def test_link_blacklist_rejected(words_dir):
    (words_dir / "domains_blacklist.txt").write_text("spam.example\n", encoding="utf-8")
    result = audit_text("来这里看看 https://spam.example/a")
    assert result.rejected
    assert "spam.example" in result.reason


def test_link_whitelist_pass(words_dir):
    (words_dir / "domains_whitelist.txt").write_text("i0.hdslb.com\n", encoding="utf-8")
    result = audit_text("看图 https://i0.hdslb.com/x.jpg")
    assert result.passed


def test_unknown_domain_needs_manual(words_dir):
    """白名单非空（严格模式）下的未知域名 → 进人工。"""
    (words_dir / "domains_whitelist.txt").write_text("i0.hdslb.com\n", encoding="utf-8")
    result = audit_text("外链 https://some-unknown.site/a")
    assert result.need_manual


def test_disable_link_check(words_dir):
    """关闭链接检查时，黑名域名不触发拒绝（纯文本场景）。"""
    (words_dir / "domains_blacklist.txt").write_text("spam.example\n", encoding="utf-8")
    result = audit_text("来这里看看 https://spam.example/a", check_link=False)
    assert result.passed


# ---------------- 词库自带分类 ----------------

def test_category_high_word_rejected(words_dir):
    """分类词库命中 → 按 levels.json 映射高风险 → 自动拒绝，带命中分类。"""
    (words_dir / _CAT_DIRNAME / "涉政类.txt").write_text("某个涉政演示词\n", encoding="utf-8")
    (words_dir / _LEVELS_FILENAME).write_text('{"涉政类": "high"}', encoding="utf-8")
    word_filter.load(force=True)

    result = audit_text("这条含某个涉政演示词")
    assert result.rejected
    assert result.hit_categories.get("涉政类") == ["某个涉政演示词"]
    assert "涉政类" in result.reason

    word_filter.load(force=True)  # 恢复


def test_category_default_medium_needs_manual(words_dir):
    """分类未在 levels.json 里 → 默认 medium → 进人工。"""
    (words_dir / _CAT_DIRNAME / "广告类.txt").write_text("某引流词\n", encoding="utf-8")
    word_filter.load(force=True)

    result = audit_text("这条含某引流词")
    assert result.need_manual
    assert result.hit_categories.get("广告类") == ["某引流词"]
    assert "广告类" in result.reason

    word_filter.load(force=True)


# ---------------- 脱敏（打码） ----------------

def test_mask_word():
    from app.services.audit.mask import mask_word

    assert mask_word("诈") == "*"
    assert mask_word("诈骗") == "诈*"
    assert mask_word("赌博平台") == "赌**台"


def test_mask_text():
    from app.services.audit.mask import mask_text

    out = mask_text("欢迎赌博平台广告", ["赌博平台", "广告"])
    assert "赌**台" in out
    assert "广告" not in out  # 命中词均已打码
    # 原文不被修改（脱敏只用于对外展示 / 通知）
    assert "赌博平台" in "欢迎赌博平台广告"


def test_reject_reason_contains_masked_words():
    """审核拒绝的 reason 里敏感词已打码（可安全用于通知 / 界面）。"""
    result = audit_text("这是一个诈骗网站")
    assert result.rejected
    assert "诈*" in result.reason
    assert "诈骗" not in result.reason
