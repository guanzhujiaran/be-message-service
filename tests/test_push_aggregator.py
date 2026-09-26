"""推送「首条直推 + 冷却期内聚合」策略的单元测试。

覆盖：
  A. 参与范围：只有失败主题（[f]）聚合，业务通知保持 1:1 直推
  B. 首条立即推送（零延迟），冷却期内的同类推送进缓冲
  C. 冷却到期 / 关闭时把缓冲发成一条摘要（按内容去重计数）
  D. 隔离：不同接收人（密钥）、不同标题各占一个桶，绝不互相合并
  E. 摘要长度硬上限；空桶回收；发送失败不建桶

运行：
    uv run pytest tests/test_push_aggregator.py -v
"""

import asyncio

import pytest

from app.models import PushChannelConfig, PushMessagePayload
from app.services.message.external import push_aggregator as aggregator


class _FakePushService:
    """PushMessageService 的替身：记录发送内容，不真的调渠道。"""

    #: 所有发送记录 (title, content)
    sent: list[tuple[str, str]] = []
    #: send 的返回值（False 模拟「无可用渠道」）
    ok: bool = True

    def __init__(self, conf, push_type=None):
        self.conf = conf
        self.push_type = push_type

    async def send(self, title: str, content: str) -> bool:
        type(self).sent.append((title, content))
        return type(self).ok


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    """每个用例都重置桶状态与替身，并替换掉真实的 PushMessageService。"""
    aggregator.reset_push_aggregator()
    _FakePushService.sent.clear()
    _FakePushService.ok = True
    monkeypatch.setattr(aggregator, "PushMessageService", _FakePushService)
    yield
    aggregator.reset_push_aggregator()
    _FakePushService.sent.clear()


def _alert(
    content: str = "队列: Q\n异常: RuntimeError: boom",
    *,
    title: str = "[f][be-bilibili-crawler@host] MQ消费失败",
    config: PushChannelConfig | None = None,
) -> PushMessagePayload:
    return PushMessagePayload(
        title=title, content=content, push_type="text", config=config
    )


def _force_due(monkeypatch) -> None:
    """把冷却期压到 0，便于测试「到期 flush」。"""
    monkeypatch.setattr(aggregator, "_cooldown_seconds", lambda: 0.0)


# ============================================================================
# A. 参与范围
# ============================================================================


def test_should_aggregate_only_failure_topic():
    assert aggregator.should_aggregate("[f][svc@host] MQ消费失败") is True
    for title in ("[i][svc@host] 数据更新", "[s][svc@host] 完成", "[w][svc@host] 警告", "无主题"):
        assert aggregator.should_aggregate(title) is False


@pytest.mark.asyncio
async def test_business_notification_is_never_aggregated():
    """业务通知（非 [f] 主题）每次都直推，不会被合并/延迟。"""
    msg = _alert(title="[i][be-bilibili-crawler@host] 抽奖数据更新")

    await aggregator.deliver(msg)
    await aggregator.deliver(msg)
    await aggregator.deliver(msg)

    assert len(_FakePushService.sent) == 3
    # 完全没有建桶，因此不存在「被缓冲/被合并」的可能
    assert aggregator._buckets == {}


# ============================================================================
# B. 首条直推 + 冷却期内缓冲
# ============================================================================


@pytest.mark.asyncio
async def test_first_message_pushed_immediately():
    """桶空闲 → 首条零延迟直推。"""
    assert await aggregator.deliver(_alert("第一条")) is True

    assert len(_FakePushService.sent) == 1
    assert _FakePushService.sent[0][1] == "第一条"


@pytest.mark.asyncio
async def test_messages_within_cooldown_are_buffered(monkeypatch):
    """冷却期内的同类推送不立即发送，攒到冷却到期才合成摘要。"""
    await aggregator.deliver(_alert("第一条"))
    await aggregator.deliver(_alert("第二条"))
    await aggregator.deliver(_alert("第三条"))

    # 只有首条发出去了
    assert len(_FakePushService.sent) == 1

    _force_due(monkeypatch)
    assert await aggregator.flush_due_buckets() == 1

    assert len(_FakePushService.sent) == 2
    digest = _FakePushService.sent[1][1]
    assert "冷却期内合并 2 条" in digest
    assert "×1  第二条" in digest
    assert "×1  第三条" in digest


@pytest.mark.asyncio
async def test_digest_counts_duplicated_contents(monkeypatch):
    """同内容的重复推送被计成 ×N（crawler 的 347 条同因失败就是靠这个压成一条）。"""
    await aggregator.deliver(_alert("第一条"))
    for _ in range(347):
        await aggregator.deliver(_alert("队列: OfficialReserveChargeLotQueue\n异常: RequestUnknownError: boom"))

    _force_due(monkeypatch)
    await aggregator.flush_due_buckets()

    digest = _FakePushService.sent[-1][1]
    assert "×347" in digest
    assert "OfficialReserveChargeLotQueue" in digest


# ============================================================================
# D. 隔离：不同接收人 / 不同标题
# ============================================================================


@pytest.mark.asyncio
async def test_different_recipients_never_share_bucket(monkeypatch):
    """不同密钥 = 不同接收人：各自直推、各自聚合，绝不混进同一条摘要。"""
    config_a = PushChannelConfig(pushme_key="user-a-key")
    config_b = PushChannelConfig(pushme_key="user-b-key")

    await aggregator.deliver(_alert("A-1", config=config_a))
    await aggregator.deliver(_alert("B-1", config=config_b))
    await aggregator.deliver(_alert("A-2", config=config_a))

    # A 首条 + B 首条 直推；A 的第二条进 A 的缓冲
    assert [content for _, content in _FakePushService.sent] == ["A-1", "B-1"]

    _force_due(monkeypatch)
    await aggregator.flush_due_buckets()

    digest = _FakePushService.sent[-1][1]
    assert "A-2" in digest
    assert "B-1" not in digest  # B 的推送不会被并进 A 的摘要


@pytest.mark.asyncio
async def test_same_credentials_via_global_fallback_share_bucket(monkeypatch):
    """桶 key 取「合并后生效的配置」：显式带 config 与回落全局只要凭据一致就是同一个桶。"""
    global_config = aggregator.settings.message_config

    # 第 1 条：显式传入与全局等价的配置（合并结果就是全局配置）
    await aggregator.deliver(_alert("显式-1", config=global_config))
    # 第 2 条：config=None → 回落全局配置（合并结果与之完全相同）
    await aggregator.deliver(_alert("回落-2", config=None))

    assert len(_FakePushService.sent) == 1  # 第 2 条进了同一个桶的缓冲

    _force_due(monkeypatch)
    await aggregator.flush_due_buckets()
    assert "回落-2" in _FakePushService.sent[-1][1]


@pytest.mark.asyncio
async def test_different_titles_have_separate_buckets():
    """同一接收人的不同类告警各占一个桶，互不挤占冷却窗口。"""
    await aggregator.deliver(_alert("消费失败-1", title="[f][svc] MQ消费失败"))
    await aggregator.deliver(_alert("服务异常-1", title="[f][svc] 服务异常"))
    await aggregator.deliver(_alert("消费失败-2", title="[f][svc] MQ消费失败"))

    assert [content for _, content in _FakePushService.sent] == ["消费失败-1", "服务异常-1"]


# ============================================================================
# C/E. 摘要长度、空桶回收、失败不建桶、关闭 flush
# ============================================================================


@pytest.mark.asyncio
async def test_digest_respects_max_len(monkeypatch):
    """摘要长度一定不超过配置上限（推送服务正文有大小限制）。"""
    monkeypatch.setattr(aggregator.settings, "push_aggregate_max_len", 300)
    _force_due(monkeypatch)

    await aggregator.deliver(_alert("第一条"))
    for index in range(200):
        await aggregator.deliver(_alert(f"内容-{index}-" + "X" * 500))
    await aggregator.flush_due_buckets()

    digest = _FakePushService.sent[-1][1]
    assert len(digest) <= 300


@pytest.mark.asyncio
async def test_expired_empty_bucket_is_recycled(monkeypatch):
    """冷却到期且没有新消息 → 回收空桶，下次同类推送可立即直推。"""
    _force_due(monkeypatch)

    await aggregator.deliver(_alert("第一条"))
    await aggregator.flush_due_buckets()  # 空桶被回收
    await aggregator.deliver(_alert("第二条"))

    assert [content for _, content in _FakePushService.sent] == ["第一条", "第二条"]


@pytest.mark.asyncio
async def test_send_failure_does_not_create_bucket():
    """首条发送失败（无可用渠道）时不建桶，后续仍会各自尝试。"""
    _FakePushService.ok = False

    assert await aggregator.deliver(_alert("第一条")) is False
    assert await aggregator.deliver(_alert("第二条")) is False

    # 两次都尝试了直推，没有被静默缓冲
    assert len(_FakePushService.sent) == 2


@pytest.mark.asyncio
async def test_stop_flushes_pending_digest(monkeypatch):
    """服务关闭时把冷却期内已聚合的摘要补发一次，避免随进程丢失。"""
    await aggregator.deliver(_alert("第一条"))
    await aggregator.deliver(_alert("第二条"))

    assert len(_FakePushService.sent) == 1

    await aggregator.stop_push_aggregator()

    assert len(_FakePushService.sent) == 2
    assert "冷却期内合并 1 条" in _FakePushService.sent[-1][1]


@pytest.mark.asyncio
async def test_sweep_loop_flushes_when_due(monkeypatch):
    """到期扫描循环会把缓冲发出去，且可正常启停（不残留后台任务）。"""
    monkeypatch.setattr(aggregator, "_sweep_seconds", lambda: 0.02)

    await aggregator.start_push_aggregator()
    try:
        await aggregator.deliver(_alert("第一条"))
        await aggregator.deliver(_alert("第二条"))
        assert len(_FakePushService.sent) == 1

        _force_due(monkeypatch)
        await asyncio.sleep(0.12)  # 等扫描循环跑一轮
        assert len(_FakePushService.sent) == 2
        assert any("冷却期内合并" in content for _, content in _FakePushService.sent)
    finally:
        await aggregator.stop_push_aggregator()
