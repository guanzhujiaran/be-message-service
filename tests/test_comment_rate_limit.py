"""评论防刷测试（2.64.0：DB 窗口计数 + root / reply 双档阈值 + 阈值热更新）。

本文件锁定的不变量：

1. **计数源是 MySQL**（`msg_comment_index` + `msg_comment_content`）：窗口内已达上限即拒，
   跨实例一致（不再有「各实例独立计数」的旁路）；软删 / 被驳回的行同样占额度
   （与每日上限 `count_created_today` 同口径）。
2. **阈值按档生效**：`root`（一级评论）与 `reply`（楼中楼回复）各一套，额度互不干扰；
   一级更严（广场刷屏的主要目标）、回复更宽松（连回多人属正常行为）。
3. **口径一致性**：`_check_rate_limit` 是唯一求值入口（由 `CommentService.add` 单一调用点
   触发，一级 `reply` 与楼中楼 `CommentBiz.reply` 都汇到这里）——若有人在路由层 / 资源类
   另起一套限流（出现两套计数源），本文件的断言应当失败。
4. **配置值是 JSON、读出来是 SQLModel**：`runtime_config.get_config_model()` 负责
   `原始 JSON → 模型实例`（缺失字段补默认、多余字段忽略、类型不符整体回落默认），
   业务侧不接触裸 dict；管理端写入经同一模型 `model_dump` 规范化。
5. **阈值可热更新**：运行时配置（`msg_sys_config['comment_rate_limit']`）覆盖 settings 默认值。

用例直接向 `msg_comment_index` / `msg_comment_content` 播种窗口内的评论行（不走完整发布
链路，避免依赖 pptr 用户数据），只验证「计数 + 求值」这段逻辑；配置侧通过替换
`runtime_config._load_raw`（原始 JSON 来源）来模拟 DB 覆盖，因此**不依赖
`msg_sys_config` 表是否已迁移**。
"""

from __future__ import annotations

import itertools
from datetime import datetime, timedelta

import pytest
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlmodel import text
from sqlmodel.ext.asyncio.session import AsyncSession as SQLModelAsyncSession

from app.core import database as db_mod
from app.core.config import settings
from app.core.database import new_session
from app.models.enums import ResourceAuditStatusEnum
from app.models.db import CommentContent, CommentIndex
from app.models.schemas import CommentRateLimitConfig
from app.services.comment.comment import _check_rate_limit
from app.services.common import runtime_config
from bili_common.models import InteractionBizTypeEnum

# 独立 mid / oid / rpid 区间，避免与其他用例互相干扰（真实 rpid 是雪花 ID，量级 1e18）
_MID_ROOT = 991_100_001
_MID_REPLY = 991_100_002
_OID = 991_100_000
# 种子 rpid 起点：只用于**避开**真实雪花 rpid 撞号，绝不用于范围删除（见 `_created_rpids`）
_RPID_BASE = 991_100_000_000

_seq = itertools.count()

#: 本文件播种过的 rpid —— 清理**只按这份精确清单删除**。
#:
#: ⚠️ 红线：绝不能按 `rpid >= _RPID_BASE` 之类的区间删除。真实 rpid 是雪花 ID（量级 1e18），
#: 远大于本文件的种子区间，区间删除会把**全站真实评论的索引 / 正文一起删掉**（曾造成事故）。
_created_rpids: list[int] = []


def _patch_config(monkeypatch: pytest.MonkeyPatch, raw: dict) -> None:
    """把库里的原始 JSON 替换成 `raw`（`None` = 库里没有该 key）。"""

    async def _fake_load_raw(session, key, default):
        if key == runtime_config.CONFIG_KEY_COMMENT_RATE_LIMIT:
            return raw if raw is not None else default
        return default

    monkeypatch.setattr(runtime_config, "_load_raw", _fake_load_raw)
    runtime_config.invalidate()


@pytest.fixture(autouse=True)
async def _bind_engine_per_test():
    """每个用例绑定当前事件循环的 engine（模块级单例跨 loop 复用会报错）。"""
    engine = create_async_engine(
        settings.mysql_message_url,
        pool_pre_ping=True,
        future=True,
        connect_args={"charset": "utf8mb4", "autocommit": False},
    )
    db_mod.engine = engine
    db_mod.async_session_maker = async_sessionmaker(
        bind=engine,
        class_=SQLModelAsyncSession,
        expire_on_commit=False,
        autoflush=False,
    )
    runtime_config.invalidate()
    await _cleanup()
    yield
    await _cleanup()
    runtime_config.invalidate()
    await engine.dispose()


async def _cleanup() -> None:
    """清掉**本文件播种的**评论行：按 `_created_rpids` 精确清单删除。

    见 `_created_rpids` 的红线说明——真实 rpid 是雪花 ID（量级 1e18），
    任何 `rpid >= 种子区间` 的删除都会连带清空全站真实评论。
    """
    if not _created_rpids:
        return
    ids = ", ".join(str(x) for x in _created_rpids)
    async with new_session() as s:
        await s.exec(text(f"DELETE FROM msg_comment_content WHERE rpid IN ({ids})"))
        await s.exec(text(f"DELETE FROM msg_comment_index WHERE rpid IN ({ids})"))
        await s.commit()
    _created_rpids.clear()


async def _seed(
    mid: int,
    message: str,
    *,
    root: int = 0,
    age_seconds: int = 0,
    audit_status: ResourceAuditStatusEnum = ResourceAuditStatusEnum.NORMAL,
) -> None:
    """播种一条「窗口内已发布」的评论（索引 + 正文），并登记 rpid 供精确清理。

    Args:
        root: 0 = 一级评论；非 0 = 楼中楼（值为根评论 rpid）。
        age_seconds: 相对当前时间的秒数（模拟滑出窗口的旧行）。
    """
    rpid = _RPID_BASE + next(_seq)
    _created_rpids.append(rpid)
    created_at = datetime.now() - timedelta(seconds=age_seconds)
    async with new_session() as s:
        s.add(
            CommentIndex(
                rpid=rpid,
                oid=_OID,
                type=InteractionBizTypeEnum.DYNAMIC,
                mid=mid,
                root=root,
                parent=root,
                auditStatus=audit_status,
                created_at=created_at,
            )
        )
        s.add(CommentContent(rpid=rpid, message=message))
        await s.commit()


# ==================== 1. 阈值口径（一级评论，settings 默认）====================


async def test_root_same_content_limited_at_threshold():
    """一级评论：同内容达上限即拒（默认 root 档 10s ≤ 2 条相同正文）。"""
    async with new_session() as s:
        for _ in range(2):
            await _seed(_MID_ROOT, "同一句话")
        with pytest.raises(ValueError, match="操作过于频繁"):
            await _check_rate_limit(s, _MID_ROOT, "同一句话", is_root=True)


async def test_root_total_window_blocks_different_content():
    """一级评论：换着内容连续刷屏同样被总量短窗拦住（默认 10s ≤ 3 条）。"""
    async with new_session() as s:
        for i in range(3):
            await _seed(_MID_ROOT, f"不同内容-{i}")
        with pytest.raises(ValueError, match="操作过于频繁"):
            await _check_rate_limit(s, _MID_ROOT, "不同内容-溢出", is_root=True)


async def test_window_expired_rows_do_not_count():
    """滑出最长窗口的旧行不计入（不会永久封禁）。"""
    async with new_session() as s:
        for _ in range(3):
            await _seed(_MID_ROOT, "很久以前", age_seconds=120)
        await _check_rate_limit(s, _MID_ROOT, "很久以前", is_root=True)


async def test_soft_hidden_rows_still_count():
    """被驳回 / 下架的行仍占额度（软删不释放窗口，与每日上限同口径）。"""
    async with new_session() as s:
        for _ in range(2):
            await _seed(
                _MID_ROOT,
                "被驳回的话",
                audit_status=ResourceAuditStatusEnum.REJECTED,
            )
        with pytest.raises(ValueError, match="操作过于频繁"):
            await _check_rate_limit(s, _MID_ROOT, "被驳回的话", is_root=True)


async def test_window_is_per_user():
    """额度按 mid 隔离：一个用户被限流不影响另一个用户。"""
    async with new_session() as s:
        for _ in range(2):
            await _seed(_MID_ROOT, "同一句话")
        await _check_rate_limit(s, 991_100_009, "同一句话", is_root=True)


# ==================== 2. 一级 / 楼中楼两档阈值互不干扰 ====================


async def test_root_and_reply_have_separate_quotas():
    """同为「同内容」规则，root 档更严、reply 档更宽松，且两者额度独立。"""
    assert (
        settings.comment_rate_reply_rules[0]["max_count"]
        > settings.comment_rate_root_rules[0]["max_count"]
    )

    async with new_session() as s:
        # 一级评论额度用尽（默认 2 条）
        for _ in range(settings.comment_rate_root_rules[0]["max_count"]):
            await _seed(_MID_ROOT, "@某个UP")
        with pytest.raises(ValueError, match="操作过于频繁"):
            await _check_rate_limit(s, _MID_ROOT, "@某个UP", is_root=True)

        # 楼中楼不受一级评论额度影响：不同 mid、不同档，正常通过
        await _check_rate_limit(s, _MID_REPLY, "@某个UP", is_root=False)


async def test_reply_quota_limited_by_its_own_threshold():
    """楼中楼按较宽的 reply 档统计：达 reply 档上限（默认 3 条）才拒。"""
    max_count = settings.comment_rate_reply_rules[0]["max_count"]
    async with new_session() as s:
        for _ in range(max_count):
            await _seed(_MID_REPLY, "回复同一句", root=12345)
        with pytest.raises(ValueError, match="操作过于频繁"):
            await _check_rate_limit(s, _MID_REPLY, "回复同一句", is_root=False)


# ==================== 3. 多规则求值（长窗 / 任意规则表）====================


async def test_long_window_rule_blocks_uniform_flood(monkeypatch):
    """长窗规则独立生效：短窗放松到 10s ≤ 100 条时，60s ≤ 3 条的长窗仍拦得住。

    用配置覆盖来构造「短窗不拦、长窗拦」的组合（settings 默认档短窗更严，长窗不会先命中）。
    """
    _patch_config(
        monkeypatch,
        {
            "root": [
                {"window_seconds": 10, "max_count": 100},
                {"window_seconds": 60, "max_count": 3},
            ],
            "reply": [],
        },
    )

    async with new_session() as s:
        for i in range(3):
            await _seed(_MID_ROOT, f"匀速-{i}", age_seconds=i * 20)
        with pytest.raises(ValueError, match="操作过于频繁"):
            await _check_rate_limit(s, _MID_ROOT, "匀速-溢出", is_root=True)


# ==================== 4. 阈值热更新与非法值回落 ====================


async def test_hot_update_overrides_defaults(monkeypatch):
    """运行时配置覆盖 settings 默认：root 档收严到 1 条、reply 档置空即关闭限流。"""
    _patch_config(
        monkeypatch,
        {
            "root": [{"window_seconds": 10, "max_count": 1, "same_content": True}],
            "reply": [],
        },
    )

    async with new_session() as s:
        await _seed(_MID_ROOT, "热更新后只允许一条")
        with pytest.raises(ValueError, match="操作过于频繁"):
            await _check_rate_limit(s, _MID_ROOT, "热更新后只允许一条", is_root=True)
        # reply 档为空 = 关闭该档限流
        for _ in range(5):
            await _seed(_MID_REPLY, "回复不受限", root=999)
        await _check_rate_limit(s, _MID_REPLY, "回复不受限", is_root=False)


async def test_invalid_config_falls_back_to_default(monkeypatch):
    """配置值非法（结构不对）时回落 settings 默认：不抛异常、也不放行到无限制。"""
    _patch_config(monkeypatch, {"root": "不是规则表", "reply": None})

    async with new_session() as s:
        for _ in range(2):
            await _seed(_MID_ROOT, "非法配置回落默认")
        with pytest.raises(ValueError, match="操作过于频繁"):
            await _check_rate_limit(s, _MID_ROOT, "非法配置回落默认", is_root=True)


# ==================== 5. JSON → SQLModel 的转换与规范化 ====================


async def test_config_is_returned_as_sqlmodel_instance(monkeypatch):
    """读取结果一定是 SQLModel 实例：缺字段补默认、多余字段被忽略。"""
    _patch_config(
        monkeypatch,
        {  # 故意只给必需字段 + 多塞一个未知字段
            "root": [{"window_seconds": 10, "max_count": 2}],
            "reply": [],
            "unknown_field": "应被忽略",
        },
    )

    async with new_session() as s:
        config = await runtime_config.get_comment_rate_limit(s)

    assert isinstance(config, CommentRateLimitConfig)
    assert config.root[0].window_seconds == 10
    assert config.root[0].max_count == 2
    assert config.root[0].same_content is False  # 模型默认值补齐
    assert config.reply == []


async def test_config_model_returns_default_instance_when_invalid(monkeypatch):
    """值非法时回落的是「校验过的默认实例」，类型与内容都可用（不是裸 dict）。"""
    _patch_config(monkeypatch, {"root": [{"window_seconds": 0, "max_count": 0}]})

    async with new_session() as s:
        config = await runtime_config.get_comment_rate_limit(s)

    assert isinstance(config, CommentRateLimitConfig)
    assert config.root == [
        type(config.root[0]).model_validate(rule)
        for rule in settings.comment_rate_root_rules
    ]


async def test_validate_config_value_normalizes_for_storage():
    """写入侧：校验通过后返回规范化 JSON（补默认值、剔多余字段），非法值抛 ValidationError。"""
    normalized = runtime_config.validate_config_value(
        runtime_config.CONFIG_KEY_COMMENT_RATE_LIMIT,
        {
            "root": [{"window_seconds": 10, "max_count": 2}],  # same_content 缺省 → False
            "reply": [{"window_seconds": 60, "max_count": 30, "same_content": True}],
            "未知字段": "剔除",
        },
    )
    assert normalized == {
        "root": [{"window_seconds": 10, "max_count": 2, "same_content": False}],
        "reply": [{"window_seconds": 60, "max_count": 30, "same_content": True}],
    }

    with pytest.raises(ValidationError):
        runtime_config.validate_config_value(
            runtime_config.CONFIG_KEY_COMMENT_RATE_LIMIT,
            {"root": [{"window_seconds": 0, "max_count": 1}]},  # window_seconds 必须 > 0
        )
    with pytest.raises(KeyError):
        runtime_config.validate_config_value("未登记的配置项", {})


async def test_runtime_config_cache_and_invalidate():
    """读取器：表未建 / 无该 key 时回落默认（且仍是模型实例）；`invalidate` 清缓存。

    `msg_sys_config` 表在测试库可能尚未建（迁移未跑）——读取失败必须回落默认值，
    这正是「配置问题不能阻断业务」的兜底，故此处断言的是回落行为而非表内容。
    """
    async with new_session() as s:
        config = await runtime_config.get_config_model(
            s, runtime_config.CONFIG_KEY_COMMENT_RATE_LIMIT, CommentRateLimitConfig
        )
    assert isinstance(config, CommentRateLimitConfig)
    assert config.root and config.reply  # 回落 settings 默认档

    runtime_config.invalidate()
    runtime_config.invalidate(runtime_config.CONFIG_KEY_COMMENT_RATE_LIMIT)
