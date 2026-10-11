"""2.66.0 机器学习推荐单测（埋点 / 精排融合 / ALS / LinUCB）。

覆盖：

- 事件服务：下发曝光流水写入（位次 + 下发分）、前端批量上报；
- LightGBM 融合：``blend_scores`` 量纲对齐与权重语义（w=0 原序、模型不可用降级）；
- ALS：冷启动用户不召回、开关关闭不召回、索引点积 top-N 有序；
- LinUCB：冷启动臂 = 纯探索（α·‖x‖）、有数据后利用项生效、上下文向量维度对齐。

纯函数部分不依赖 DB；事件写入复用真实 MySQL 主库（与本模块其它用例一致）。
"""

import datetime

import numpy as np
import pytest
from pydantic import ValidationError
from sqlmodel import text

from app.core.config import settings
from app.core.database import new_session
from app.models.enums import FeedEventTypeEnum
from app.models.schemas.ml_status import FeedMLStatusResp, MLModelStatus
from app.models.schemas.sys_config import FeedMLConfig
from app.services.moment.als_recall import ALSIndex
from app.services.moment.feed_event import FeedEventItem, FeedEventService
from app.services.moment.linucb import (
    ArmContext,
    blend_ucb,
    context_vector,
    feature_dim,
    ucb_score,
)
from app.services.moment.ml_ranker import blend_scores

E_MID = 920301


def test_blend_scores_zero_weight_keeps_base():
    """w<=0 / 模型不可用 → 原样返回 EdgeRank 分（灰度可一键回退）。"""
    base = [3.0, 2.0, 1.0]
    assert blend_scores(base, [0.9, 0.1, 0.5], weight=0) == base
    assert blend_scores(base, None) == base
    assert blend_scores(base, []) == base


def test_blend_scores_aligns_scale():
    """融合权重语义：w=1 时结果均值 ≈ 原分均值（量纲对齐，不整体抬升/压低）。"""
    base = [10.0, 8.0, 6.0, 4.0]
    ml = [0.9, 0.7, 0.5, 0.3]
    out = blend_scores(base, ml, weight=1.0)
    assert sum(out) / len(out) == pytest.approx(sum(base) / len(base), rel=0.05)
    # 顺序仍由模型分主导（高模型分排前）
    assert out == sorted(out, reverse=True)


def test_linucb_cold_start_is_pure_exploration():
    """冷启动（无臂）→ θ=0，ucb = α·‖x‖：特征越强越先被探索。"""
    strong = context_vector(
        ArmContext(is_rich=True, topic_count=3, text_len=200, engagement=0.5)
    )
    weak = context_vector(ArmContext())
    assert ucb_score(strong) > ucb_score(weak) > 0


def test_linucb_exploit_grows_with_reward():
    """同一上下文下，正奖励越多 → 利用项（θᵀx）越大，UCB 越高。"""
    x = context_vector(ArmContext(is_rich=True, text_len=120))
    d = feature_dim()
    a = np.eye(d)
    low = ucb_score(x, a_matrix=a, b_vector=np.zeros(d))
    high = ucb_score(x, a_matrix=a, b_vector=np.full(d, 3.0))
    assert high > low


def test_linucb_blend_is_zero_mean():
    """探索分零均值化后叠加：不改变整体量级，只改相对顺序。"""
    base = [5.0, 5.0, 5.0]
    out = blend_ucb(base, [0.1, 0.9, 0.5])
    assert sum(out) == pytest.approx(15.0, abs=1e-6)
    assert out[1] > out[2] > out[0]


def test_als_index_cold_user_returns_empty():
    """冷启动用户（不在索引里）不召回；limit<=0 返回空。"""
    index = ALSIndex(
        user_ids=np.asarray([1, 2, 3], dtype=np.int64),
        item_ids=np.asarray([10, 20], dtype=np.int64),
        user_factors=np.asarray([[1.0], [0.0], [0.5]], dtype=np.float32),
        item_factors=np.asarray([[1.0], [0.2]], dtype=np.float32),
    )
    assert index.recall(999, 10) == []
    assert index.recall(1, 0) == []


def test_als_index_ranks_by_inner_product():
    """召回按「用户向量 · 物品向量」降序（隐语义兴趣度）。"""
    index = ALSIndex(
        user_ids=np.asarray([7], dtype=np.int64),
        item_ids=np.asarray([10, 20, 30], dtype=np.int64),
        user_factors=np.asarray([[1.0, 0.0]], dtype=np.float32),
        item_factors=np.asarray([[0.5, 0.0], [0.9, 0.0], [0.1, 0.0]], dtype=np.float32),
    )
    assert index.recall(7, 3) == [20, 10, 30]
    assert index.recall(7, 2) == [20, 10]


async def test_feed_event_records_impression_and_client_events():
    """埋点：下发写 impression（含位次/下发分），前端上报 click/dwell。"""
    viewer = "mid:920301"
    async with new_session() as s:
        await s.exec(text(f"DELETE FROM TFeedEventLog WHERE viewerKey='{viewer}'"))
        await s.commit()

        await FeedEventService.record_impressions(
            s,
            viewer_key=viewer,
            biz_type=1,
            feed_scene="comprehensive",
            biz_ids=[101, 102],
            now=datetime.datetime.now(),
        )
        rows = (
            await s.exec(
                text(
                    f"SELECT bizId, event, position FROM TFeedEventLog "
                    f"WHERE viewerKey='{viewer}' ORDER BY position"
                )
            )
        ).all()
        assert [(int(r[0]), str(r[1]), int(r[2])) for r in rows] == [
            (101, "IMPRESSION", 0),
            (102, "IMPRESSION", 1),
        ]

        written = await FeedEventService.report_client_events(
            s,
            viewer_key=viewer,
            biz_type=1,
            feed_scene="comprehensive",
            items=[
                FeedEventItem(biz_id=101, event=FeedEventTypeEnum.CLICK, position=0),
                FeedEventItem(biz_id=101, event=FeedEventTypeEnum.DWELL, value_ms=4500),
            ],
        )
        assert written == 2

        clicks = (
            await s.exec(
                text(
                    f"SELECT event, valueMs FROM TFeedEventLog WHERE viewerKey='{viewer}' "
                    f"AND bizId=101 AND event IN ('CLICK','DWELL') ORDER BY event"
                )
            )
        ).all()
        assert [(str(r[0]), r[1]) for r in clicks] == [("CLICK", None), ("DWELL", 4500)]

        await s.exec(text(f"DELETE FROM TFeedEventLog WHERE viewerKey='{viewer}'"))
        await s.commit()


def test_ml_switches_default_off():
    """默认全关：机器学习三件套不影响线上行为（需训练产出后显式开启）。"""
    assert settings.ml_ranker_enabled is False
    assert settings.ml_als_enabled is False
    assert settings.ml_linucb_enabled is False


def test_feed_ml_config_registered_and_validated():
    """运行时配置：feed_ml 已登记、可校验、默认全关（管理端可热更新开关）。"""
    from app.services.common.runtime_config import (
        CONFIG_KEY_FEED_ML,
        CONFIG_SPECS,
        validate_config_value,
    )

    assert CONFIG_KEY_FEED_ML in CONFIG_SPECS
    # 默认值回落 settings（当前全关）
    default = FeedMLConfig.model_validate(CONFIG_SPECS[CONFIG_KEY_FEED_ML].default())
    assert default.ranker_enabled is False
    assert default.als_enabled is False
    assert default.linucb_enabled is False
    # 写入前规范化：补齐缺失字段、剔除多余字段
    normalized = validate_config_value(
        CONFIG_KEY_FEED_ML, {"ranker_enabled": True, "unknown": 1}
    )
    assert normalized["ranker_enabled"] is True
    assert "unknown" not in normalized
    assert normalized["als_limit"] == settings.ml_als_limit
    # 越界值被拒（管理端写入会 400）
    with pytest.raises(ValidationError):
        validate_config_value(CONFIG_KEY_FEED_ML, {"ranker_blend_weight": 5.0})


def test_feed_ml_config_rejects_unknown_key():
    """未登记的 key 不允许写入（避免无人消费的死配置）。"""
    from app.services.common.runtime_config import validate_config_value

    with pytest.raises(KeyError):
        validate_config_value("ml_not_registered", {"a": 1})


def test_ml_status_model_defaults():
    """状态模型：未训练时 ready=False / metrics=None（前端据此显示未就绪）。"""
    st = MLModelStatus()
    assert st.ready is False
    assert st.metrics is None
    resp = FeedMLStatusResp(config=FeedMLConfig(), ranker=st, als=st)
    assert resp.linucbArms == 0 and resp.eventSamples == 0
