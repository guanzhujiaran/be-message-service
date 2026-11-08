"""管理端：be-crawler 云端 LLM 配置代理服务（2.7x）。

职责只有四件事：

1. **读**：代理 crawler `GET /llm/config` + `GET /llm/stats`，把内部 dict 归一成本服务
   的响应模型（数值转字符串，时间戳转秒级整数字符串）；
2. **写**：整体覆盖 `PUT /llm/config`，写入前做参数校验；`token` 传脱敏值 / 空串时
   **保留原密钥**（crawler 读取态返回的就是脱敏值，前端原样回传是正常操作）；
3. **回退**：`DELETE /llm/config/override` 让 crawler 删除本地持久化文件
   （`runtime/llm_apis.json`）并热切回 `.env`，用于「撤销所有人工保存」；
4. **探测**：代理 `POST /llm/config/test`，对单条实例做一次最小 completion，
   只回答「现在还能不能用」（不参与状态机、不改统计）；
5. **审计**：写 / 探测操作打一条带操作人 mid 的 info 日志（与其它管理端写接口口径一致）。

为什么不做缓存：配置持久化在 crawler 的 `runtime/llm_apis.json`（事实来源：文件 > `.env`），
任何缓存都会让管理端看到过期数据；量级极小（个位数实例），实时转发即可。
"""

from loguru import logger

from app.models.schemas.crawler_llm import (
    CrawlerLlmConfigItem,
    CrawlerLlmConfigResp,
    CrawlerLlmSaveReq,
    CrawlerLlmStatsItem,
    CrawlerLlmTestReq,
    CrawlerLlmTestResp,
)
from app.services.infrastructure.crawler_llm import (
    CrawlerLlmClient,
    CrawlerLlmConfig,
    CrawlerLlmError,
    CrawlerLlmStatsRow,
    get_crawler_llm_client,
)

__all__ = ["CrawlerLlmService", "CrawlerLlmValueError"]

#: 脱敏标记：crawler 侧 `_mask_token` 生成的串一定含它
_MASK_FLAG = "****"


class CrawlerLlmValueError(ValueError):
    """入参非法（接口层映射为 400 / `INVALID_PARAM`）。"""


class CrawlerLlmService:
    """crawler LLM 配置的管理端编排（只做代理 + 校验 + 归一）。"""

    @staticmethod
    def _client() -> CrawlerLlmClient:
        return get_crawler_llm_client()

    @staticmethod
    def _to_item(index: int, cfg: CrawlerLlmConfig) -> CrawlerLlmConfigItem:
        return CrawlerLlmConfigItem(
            index=str(index),
            base_url=cfg.base_url,
            model_name=cfg.model_name,
            token=cfg.token,
            requests_per_second=str(cfg.requests_per_second),
        )

    @staticmethod
    def _to_stat(row: CrawlerLlmStatsRow) -> CrawlerLlmStatsItem:
        attempts = int(row.success_count) + int(row.failure_count)
        rate = (int(row.success_count) / attempts) if attempts > 0 else 0.0
        return CrawlerLlmStatsItem(
            base_url=row.base_url,
            model_name=row.model,
            state=row.state,
            available=bool(row.available),
            disabled=bool(row.disabled),
            disabled_reason=row.disabled_reason,
            invoke_count=str(row.invoke_count),
            success_count=str(row.success_count),
            failure_count=str(row.failure_count),
            consecutive_failures=str(row.consecutive_failures),
            success_rate=f"{rate:.2f}",
            rate_per_minute=str(row.rate_per_minute),
            avg_latency_seconds=f"{float(row.avg_latency_seconds or 0.0):.3f}",
            total_tokens=str(row.total_tokens),
            last_used_at=_ts_or_none(row.last_used_at),
            resume_at=_ts_or_none(row.resume_at),
            last_error=row.last_error,
        )

    @staticmethod
    async def _load_stats() -> list[CrawlerLlmStatsItem]:
        """统计是弱依赖：失败只告警并返回空列表，不影响配置读写。"""
        try:
            rows = await CrawlerLlmService._client().get_stats()
        except CrawlerLlmError as e:
            logger.warning(f"crawler LLM 统计读取失败，返回空统计：{e}")
            return []
        return [CrawlerLlmService._to_stat(row) for row in rows]

    @staticmethod
    async def get_config() -> CrawlerLlmConfigResp:
        """读取当前配置 + 运行统计（页面一次请求即可渲染）。"""
        configs = await CrawlerLlmService._client().get_configs()
        items = [
            CrawlerLlmService._to_item(i, cfg) for i, cfg in enumerate(configs)
        ]
        return CrawlerLlmConfigResp(
            items=items, stats=await CrawlerLlmService._load_stats()
        )

    @staticmethod
    async def test_config(
        req: CrawlerLlmTestReq, operator_mid: int
    ) -> CrawlerLlmTestResp:
        """对单条实例做一次连通性探测（可测未保存的编辑态）。

        只做一次最小 completion（crawler 侧 `max_tokens=1`），
        **不参与健康状态机、不改统计、不落盘** —— 纯诊断。

        Args:
            req: 待测实例的可编辑字段（`token` 为空/脱敏时按 `index` 沿用原密钥）。
            operator_mid: 操作管理员 mid（仅用于审计日志）。

        Raises:
            CrawlerLlmValueError: `index` 不是数字（前端传的是字符串）。
            CrawlerLlmError: crawler 不可达 / 非 2xx。
        """
        index: int | None = None
        raw_index = (req.index or "").strip()
        if raw_index:
            try:
                index = int(raw_index)
            except ValueError as e:
                raise CrawlerLlmValueError(f"索引必须是数字：{raw_index}") from e

        row = await CrawlerLlmService._client().test_config(
            index=index,
            base_url=req.base_url.strip(),
            model_name=req.model_name.strip(),
            token=req.token.strip(),
        )
        logger.info(
            f"管理员 {operator_mid} 测试 crawler LLM 实例：{row.model_name}"
            f"（{row.base_url}）→ {'成功' if row.ok else '失败'}"
            f"（status={row.status_code}，{row.latency_ms}ms）"
        )
        return CrawlerLlmTestResp(
            ok=row.ok,
            base_url=row.base_url,
            model_name=row.model_name,
            status_code=None if row.status_code is None else str(row.status_code),
            latency_ms=str(row.latency_ms),
            error=row.error,
        )

    @staticmethod
    async def reset_config(operator_mid: int) -> CrawlerLlmConfigResp:
        """撤销人工保存：让 crawler 删除持久化文件并热切回 `.env`。

        返回切换后的配置 + 统计（复用 `get_config()`，口径与 GET 完全一致）。
        幂等：文件本来就不存在时 crawler 也返回成功（只是把 `.env` 重新应用一遍）。

        Args:
            operator_mid: 操作管理员 mid（仅用于审计日志）。

        Raises:
            CrawlerLlmError: crawler 不可达 / 非 2xx。
        """
        await CrawlerLlmService._client().reset_configs()
        logger.info(
            f"管理员 {operator_mid} 撤销 crawler LLM 人工保存，已切回 .env 配置"
        )
        return await CrawlerLlmService.get_config()

    @staticmethod
    async def save_config(
        req: CrawlerLlmSaveReq, operator_mid: int
    ) -> CrawlerLlmConfigResp:
        """整体覆盖保存（热更新 + 落盘，无需重启 crawler）。

        Args:
            req: 新的实例列表（顺序即索引顺序）。
            operator_mid: 操作管理员 mid（仅用于审计日志）。

        Raises:
            CrawlerLlmValueError: 入参非法（字段缺失 / 值为空 / 新增实例未填密钥）。
        """
        if not req.items:
            raise CrawlerLlmValueError("至少需要保留一个 LLM 实例")

        # 读一次旧配置：脱敏 token 需要按索引回填原密钥
        existing = await CrawlerLlmService._client().get_configs()

        configs: list[CrawlerLlmConfig] = []
        for i, item in enumerate(req.items):
            base_url = item.base_url.strip()
            model_name = item.model_name.strip()
            raw_rps = item.requests_per_second.strip()
            if not base_url.startswith(("http://", "https://")):
                raise CrawlerLlmValueError(
                    f"第 {i + 1} 个实例的接口地址必须以 http:// 或 https:// 开头"
                )
            if not model_name:
                raise CrawlerLlmValueError(f"第 {i + 1} 个实例缺少模型名")
            try:
                rps = float(raw_rps)
            except ValueError as e:
                raise CrawlerLlmValueError(
                    f"第 {i + 1} 个实例的限流必须是数字（如 0.5 表示 2 秒 1 次）"
                ) from e
            if rps <= 0:
                raise CrawlerLlmValueError(f"第 {i + 1} 个实例的限流必须大于 0")

            token = item.token.strip()
            if not token or _MASK_FLAG in token:
                if i >= len(existing):
                    raise CrawlerLlmValueError(f"第 {i + 1} 个实例是新实例，必须填写密钥")
                token = existing[i].token

            configs.append(
                CrawlerLlmConfig(
                    base_url=base_url,
                    model_name=model_name,
                    token=token,
                    requests_per_second=rps,
                )
            )

        saved = await CrawlerLlmService._client().replace_configs(configs)
        logger.info(
            f"管理员 {operator_mid} 更新 crawler LLM 配置："
            f"{len(saved)} 个实例（{', '.join(c.model_name for c in saved)}）"
        )
        items = [CrawlerLlmService._to_item(i, cfg) for i, cfg in enumerate(saved)]
        return CrawlerLlmConfigResp(
            items=items, stats=await CrawlerLlmService._load_stats()
        )


def _ts_or_none(raw: float | None) -> str | None:
    """float 时间戳 → 秒级整数字符串（对外统一秒级 int，禁止 ISO 字符串）。"""
    if raw is None:
        return None
    return str(int(raw))
