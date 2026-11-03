"""管理端 crawler LLM 配置代理的单元测试（不依赖 crawler / 数据库）。

覆盖点：
1. **脱敏 token 回填**：保存时回传脱敏值或空串必须沿用原密钥，不能把 `****` 写进配置；
2. **新增实例必须填密钥**：新行留空直接 400，不发请求；
3. **入参校验**：base_url 协议 / 模型名 / 限流必须为正数；
4. **统计归一化**：数值与时间戳一律转字符串，秒级时间戳取整，成功率/平均耗时格式化；
5. **弱依赖**：统计接口失败不影响配置读取。
"""

from typing import Any

import pytest

from app.models.schemas.crawler_llm import CrawlerLlmSaveItem, CrawlerLlmSaveReq
from app.services.admin import crawler_llm as service_mod
from app.services.admin.crawler_llm import CrawlerLlmService, CrawlerLlmValueError
from app.services.infrastructure.crawler_llm import (
    CrawlerLlmConfig,
    CrawlerLlmError,
    CrawlerLlmStatsRow,
)


class _FakeClient:
    """记录调用参数的假 crawler 客户端。"""

    def __init__(
        self,
        *,
        configs: list[CrawlerLlmConfig] | None = None,
        stats: list[CrawlerLlmStatsRow] | None = None,
        stats_error: Exception | None = None,
    ) -> None:
        self.configs = configs or []
        self.stats = stats or []
        self.stats_error = stats_error
        self.saved: list[CrawlerLlmConfig] | None = None

    async def get_configs(self) -> list[CrawlerLlmConfig]:
        return self.configs

    async def replace_configs(
        self, configs: list[CrawlerLlmConfig]
    ) -> list[CrawlerLlmConfig]:
        self.saved = configs
        return configs

    async def get_stats(self) -> list[CrawlerLlmStatsRow]:
        if self.stats_error is not None:
            raise self.stats_error
        return self.stats


@pytest.fixture()
def use_client(monkeypatch: pytest.MonkeyPatch):
    """把服务层的客户端替换成给定假实现。"""

    def _install(client: _FakeClient) -> _FakeClient:
        monkeypatch.setattr(service_mod, "get_crawler_llm_client", lambda: client)
        return client

    return _install


def _save_item(
    *,
    base_url: str = "https://api.example.com/v1",
    model_name: str = "deepseek-chat",
    token: str = "",
    rps: str = "0.5",
) -> CrawlerLlmSaveItem:
    return CrawlerLlmSaveItem(
        base_url=base_url,
        model_name=model_name,
        token=token,
        requests_per_second=rps,
    )


class TestMaskedTokenIsPreserved:
    """脱敏 token 不能覆盖真实密钥。"""

    @pytest.mark.asyncio
    async def test_masked_token_keeps_original(self, use_client: Any) -> None:
        client = use_client(
            _FakeClient(
                configs=[
                    CrawlerLlmConfig(
                        base_url="https://api.example.com/v1",
                        model_name="deepseek-chat",
                        token="sk-real-token-1234",
                        requests_per_second=0.5,
                    )
                ]
            )
        )
        req = CrawlerLlmSaveReq(
            items=[_save_item(token="sk-r****1234")]  # 前端原样回传脱敏值
        )

        await CrawlerLlmService.save_config(req, operator_mid=1)

        assert client.saved is not None
        assert client.saved[0].token == "sk-real-token-1234"

    @pytest.mark.asyncio
    async def test_empty_token_keeps_original(self, use_client: Any) -> None:
        client = use_client(
            _FakeClient(
                configs=[
                    CrawlerLlmConfig(
                        base_url="https://api.example.com/v1",
                        model_name="deepseek-chat",
                        token="sk-real-token-1234",
                        requests_per_second=0.5,
                    )
                ]
            )
        )

        await CrawlerLlmService.save_config(
            CrawlerLlmSaveReq(items=[_save_item(token="")]), operator_mid=1
        )

        assert client.saved is not None
        assert client.saved[0].token == "sk-real-token-1234"

    @pytest.mark.asyncio
    async def test_plain_token_overrides(self, use_client: Any) -> None:
        client = use_client(
            _FakeClient(
                configs=[
                    CrawlerLlmConfig(
                        base_url="https://api.example.com/v1",
                        model_name="deepseek-chat",
                        token="old-token",
                        requests_per_second=0.5,
                    )
                ]
            )
        )

        await CrawlerLlmService.save_config(
            CrawlerLlmSaveReq(items=[_save_item(token="new-token")]), operator_mid=1
        )

        assert client.saved is not None
        assert client.saved[0].token == "new-token"

    @pytest.mark.asyncio
    async def test_new_item_without_token_rejected(self, use_client: Any) -> None:
        client = use_client(_FakeClient(configs=[]))

        with pytest.raises(CrawlerLlmValueError, match="必须填写密钥"):
            await CrawlerLlmService.save_config(
                CrawlerLlmSaveReq(items=[_save_item(token="")]), operator_mid=1
            )
        # 校验失败不能落库
        assert client.saved is None


class TestValidation:
    """入参校验（都不应发出写请求）。"""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("item", "expected"),
        [
            (_save_item(base_url="api.example.com"), "http:// 或 https://"),
            (_save_item(model_name="  "), "缺少模型名"),
            (_save_item(rps="abc"), "限流必须是数字"),
            (_save_item(rps="0"), "限流必须大于 0"),
        ],
    )
    async def test_invalid_items(
        self, use_client: Any, item: CrawlerLlmSaveItem, expected: str
    ) -> None:
        client = use_client(
            _FakeClient(configs=[CrawlerLlmConfig(token="t", requests_per_second=1.0)])
        )
        with pytest.raises(CrawlerLlmValueError, match=expected):
            await CrawlerLlmService.save_config(
                CrawlerLlmSaveReq(items=[item]), operator_mid=1
            )
        assert client.saved is None

    @pytest.mark.asyncio
    async def test_empty_items_rejected(self, use_client: Any) -> None:
        use_client(_FakeClient())
        with pytest.raises(CrawlerLlmValueError, match="至少需要保留一个"):
            await CrawlerLlmService.save_config(
                CrawlerLlmSaveReq(items=[]), operator_mid=1
            )


class TestStatsNormalization:
    """统计字段全部转字符串、时间戳取整。"""

    @pytest.mark.asyncio
    async def test_stats_are_stringified(self, use_client: Any) -> None:
        use_client(
            _FakeClient(
                configs=[CrawlerLlmConfig(token="****", requests_per_second=0.5)],
                stats=[
                    CrawlerLlmStatsRow(
                        model="deepseek-chat",
                        base_url="https://api.example.com/v1",
                        state="healthy",
                        available=True,
                        invoke_count=10,
                        success_count=9,
                        failure_count=1,
                        consecutive_failures=0,
                        rate_per_minute=2,
                        avg_latency_seconds=1.23456,
                        total_tokens=1234,
                        last_used_at=1762000000.75,
                        resume_at=1762000060.4,
                        last_error="429 rate limited",
                    )
                ],
            )
        )

        resp = await CrawlerLlmService.get_config()

        stat = resp.stats[0]
        assert stat.model_name == "deepseek-chat"
        assert stat.available is True
        assert stat.invoke_count == "10"
        assert stat.success_rate == "0.90"
        assert stat.avg_latency_seconds == "1.235"
        assert stat.total_tokens == "1234"
        assert stat.last_used_at == "1762000000"
        assert stat.resume_at == "1762000060"
        assert resp.items[0].requests_per_second == "0.5"

    @pytest.mark.asyncio
    async def test_stats_failure_degrades(self, use_client: Any) -> None:
        use_client(
            _FakeClient(
                configs=[CrawlerLlmConfig(token="****", requests_per_second=0.5)],
                stats_error=CrawlerLlmError("crawler 不可达"),
            )
        )

        resp = await CrawlerLlmService.get_config()

        assert resp.items  # 配置照常返回
        assert resp.stats == []
