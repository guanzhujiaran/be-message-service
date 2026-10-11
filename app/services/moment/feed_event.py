"""Feed 行为事件服务（2.66.0，机器学习推荐 P0 埋点）。

职责：

1. **写入**：服务端下发后写 ``impression`` 流水（含位次与下发分）；前端上报
   ``click`` / ``dwell``（批量，匿名可报）；
2. **读取**：供离线样本构建按资源 / 观众回放事件（``scripts/build_feed_samples.py``）。

设计约束：

- **只追加**：事件流水不更新不删除，与去重用的 ``TFeedImpression`` 职责分离；
- **弱依赖**：写失败只告警不抛（埋点不能拖垮 Feed 读路径）；
- **批量写**：下发曝光一次 ``INSERT`` 多行，前端上报按批限制条数（防滥用）。
"""

from dataclasses import dataclass
from datetime import datetime

from bili_common.models import InteractionBizTypeEnum
from loguru import logger

from app.models.db import TFeedEventLog
from app.models.enums import FeedEventTypeEnum
from app.services.common.indexed import Indexed


@dataclass(frozen=True, slots=True)
class FeedEventItem:
    """单条待写事件（轻量值对象，避免调用方拼 ORM 行）。"""

    biz_id: int
    event: FeedEventTypeEnum
    position: int | None = None
    score: float | None = None
    value_ms: int | None = None


class FeedEventService:
    """Feed 行为事件写入 / 查询（静态方法集合，无状态）。"""

    @staticmethod
    async def record_impressions(
        session,
        *,
        viewer_key: str,
        biz_type: InteractionBizTypeEnum,
        feed_scene: str,
        biz_ids: list[int],
        scores: Indexed[float] | None = None,
        now: datetime | None = None,
    ) -> None:
        """下发后批量写曝光流水（一次 INSERT 多行，弱依赖失败仅告警）。

        ``biz_ids`` 的顺序即下发位次（写入 ``position``），用于位置偏差特征与
        离线「下发分 vs 实际互动」对照。
        """
        if not biz_ids:
            return
        ts = now or datetime.now()
        session.add_all(
            [
                TFeedEventLog(
                    viewerKey=viewer_key,
                    bizType=biz_type,
                    bizId=int(bid),
                    event=FeedEventTypeEnum.IMPRESSION,
                    feedScene=feed_scene,
                    position=idx,
                    score=scores.value_of(int(bid)) if scores else None,
                    created_at=ts,
                    updated_at=ts,
                )
                for idx, bid in enumerate(biz_ids)
            ]
        )
        try:
            await session.commit()
        except Exception as e:  # pragma: no cover - 埋点失败不影响主流程
            logger.warning(f"[feed_event] 写入曝光流水失败，忽略: {e}")

    @staticmethod
    async def report_client_events(
        session,
        *,
        viewer_key: str,
        biz_type: InteractionBizTypeEnum,
        feed_scene: str,
        items: list[FeedEventItem],
        now: datetime | None = None,
    ) -> int:
        """前端批量上报 click / dwell（返回写入条数；失败返回 0，不抛）。"""
        if not items:
            return 0
        ts = now or datetime.now()
        session.add_all(
            [
                TFeedEventLog(
                    viewerKey=viewer_key,
                    bizType=biz_type,
                    bizId=int(it.biz_id),
                    event=it.event,
                    feedScene=feed_scene,
                    position=it.position,
                    score=None,
                    valueMs=it.value_ms,
                    created_at=ts,
                    updated_at=ts,
                )
                for it in items
            ]
        )
        try:
            await session.commit()
            return len(items)
        except Exception as e:  # pragma: no cover - 埋点失败不影响主流程
            logger.warning(f"[feed_event] 写入前端上报事件失败，忽略: {e}")
            return 0


__all__ = ["FeedEventItem", "FeedEventService"]
