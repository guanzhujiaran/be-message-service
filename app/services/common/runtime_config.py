"""运行时可热更新配置的通用读取器（2.64.0）。

设计说明：

- **存的是 JSON，读出来是 SQLModel**：`msg_sys_config.value` 是 JSON 列，各配置项在
  `CONFIG_SPECS` 登记「值模型（SQLModel）+ settings 默认值提供者」；`get_config_model()`
  负责 `原始 JSON → model_validate → 模型实例`，因此调用方拿到的一定是**校验过的强类型
  对象**（缺失字段补模型默认、多余字段忽略、类型不符整体回落默认），不需要自己去
  `dict.get()` 和转换。写入侧复用同一个模型做 `model_dump(mode="json")` 规范化。
- **DB 是权威，进程内缓存只为省查询**：缓存的是**原始 JSON**（不是模型实例——避免跨请求
  共享可变对象），TTL（`settings.sys_config_cache_ttl_seconds`，默认 10s）内不再查库。
  多实例各自持缓存，故「改完配置 → 全实例一致」有 ≤ TTL 的窗口；要秒级一致可在既有
  `message_exchange`（TOPIC）上加广播调用 `invalidate(key)`，读取路径无需改动。
- **绝不因配置读不到而阻断业务**：表未建 / DB 异常 / 值非法 → 一律回落 settings 默认值，
  且回落路径**同样经过模型校验**，保证返回类型一致、默认值必定完整。
- **读取必须用 SAVEPOINT 包裹**（`begin_nested`）：失败只回滚到保存点，不会把调用方的
  业务事务打成失败态（该读取发生在评论发布主流程内）。
- 与 `daily_limit` 一样是「自治接入」：各业务按需调用，并非所有参数都必须搬进来。
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, TypeVar

from loguru import logger
from pydantic import ValidationError
from sqlmodel import SQLModel
from sqlmodel.ext.asyncio.session import AsyncSession

from app.core.config import settings
from app.models.db.sys_config_tbl import SysConfig
from app.models.schemas.sys_config import CommentRateLimitConfig

#: 评论频率限制配置键（值模型 `CommentRateLimitConfig`）
CONFIG_KEY_COMMENT_RATE_LIMIT = "comment_rate_limit"

#: 配置值模型类型变量（`get_config_model` 的返回类型）
T = TypeVar("T", bound=SQLModel)


def _default_comment_rate_limit() -> dict[str, Any]:
    """评论限流默认值（DB 无配置 / 值非法时使用），形态与库里 JSON 一致。"""
    return {
        "root": settings.comment_rate_root_rules,
        "reply": settings.comment_rate_reply_rules,
    }


@dataclass(frozen=True)
class ConfigSpec:
    """一个可热更新配置项的登记项。

    Attributes:
        model: 值模型（SQLModel）——JSON ↔ 模型转换与校验的唯一依据。
        default: 默认值提供者（返回 JSON 形态的 dict，通常是 settings 里的兜底值）。
    """

    model: type[SQLModel]
    default: Callable[[], dict[str, Any]]


#: key → 登记项（读写共用）。未登记的 key 不允许写入，也不能读取。
CONFIG_SPECS: dict[str, ConfigSpec] = {
    CONFIG_KEY_COMMENT_RATE_LIMIT: ConfigSpec(
        model=CommentRateLimitConfig, default=_default_comment_rate_limit
    ),
}

#: 进程内缓存：key → (写入缓存时刻, 原始 JSON)
_cache: dict[str, tuple[float, Any]] = {}


def invalidate(key: str | None = None) -> None:
    """清本实例缓存（`None` = 全部）。

    写路径在提交事务后调用，使**本实例立即生效**；其余实例靠 TTL 过期自然刷新。
    """
    if key is None:
        _cache.clear()
    else:
        _cache.pop(key, None)


def _ttl() -> float:
    return max(float(settings.sys_config_cache_ttl_seconds), 0.0)


async def _load_raw(session: AsyncSession, key: str, default: dict[str, Any]) -> Any:
    """取配置的**原始 JSON**：TTL 缓存 → 主库；表未建 / 异常回落 `default`。

    `SAVEPOINT`（`begin_nested`）保证读取失败只回滚到保存点，不污染调用方事务；
    异常路径同样写缓存（TTL 内不再重试），避免「表未建」时每次发布都失败查库 + 刷日志。
    """
    now = time.monotonic()
    hit = _cache.get(key)
    if hit is not None and now - hit[0] < _ttl():
        return hit[1]
    try:
        async with session.begin_nested():
            row = await session.get(SysConfig, key)
    except Exception as e:  # noqa: BLE001 - 配置读取失败不能阻断业务
        logger.warning(f"[RuntimeConfig] 读取 {key} 失败，回落默认值: {e}")
        _cache[key] = (now, default)
        return default
    value = row.value if row is not None else default
    _cache[key] = (now, value)
    return value


async def get_config_model(session: AsyncSession, key: str, model: type[T]) -> T:
    """读取配置并转换成 SQLModel 实例（DB 覆盖 → settings 默认 → 值非法回落默认）。

    转换语义由 `model` 决定：缺失字段补模型默认值、多余字段忽略、类型不符则**整体**回落
    默认值（并打 warning，便于发现脏配置）。

    Raises:
        KeyError: 该 key 未登记（调用方应使用 `CONFIG_KEY_*` 常量）。
    """
    spec = CONFIG_SPECS.get(key)
    if spec is None:
        raise KeyError(f"未登记的配置项: {key}")
    default = spec.default()
    raw = await _load_raw(session, key, default)
    try:
        return model.model_validate(raw)
    except ValidationError as e:
        logger.warning(f"[RuntimeConfig] {key} 值非法，回落默认值: {e}")
        return model.model_validate(default)


async def get_comment_rate_limit(session: AsyncSession) -> CommentRateLimitConfig:
    """读取评论频率限制配置（强类型实例；不存在 / 非法时回落 settings 默认值）。"""
    return await get_config_model(
        session, CONFIG_KEY_COMMENT_RATE_LIMIT, CommentRateLimitConfig
    )


def validate_config_value(key: str, value: dict[str, Any]) -> dict[str, Any]:
    """按 key 校验并**规范化**配置值（管理端写入前调用）。

    Returns:
        规范化后的 JSON（`model_dump(mode="json")`：补齐模型默认值、剔除多余字段），
        直接落库即可——库里存的始终是「模型认可的结构」。

    Raises:
        KeyError: 该 key 未登记（不允许写入无人消费的配置）。
        ValidationError: 值结构非法。
    """
    spec = CONFIG_SPECS[key]
    return spec.model.model_validate(value).model_dump(mode="json")


__all__ = [
    "CONFIG_KEY_COMMENT_RATE_LIMIT",
    "CONFIG_SPECS",
    "ConfigSpec",
    "get_comment_rate_limit",
    "get_config_model",
    "invalidate",
    "validate_config_value",
]
