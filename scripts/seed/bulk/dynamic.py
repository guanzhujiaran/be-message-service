"""大数据灌数：单条动态（创建 → 审核 → 按分布点赞/浏览 + 评论套件）。"""
import asyncio
from itertools import cycle

from bili_common.models import InteractionBizTypeEnum
from loguru import logger

from ..client import SeedClient
from ..helpers import _at_nodes, _at_targets_deterministic
from ..rr import _rr, _sample
from .comment import _seed_bulk_comment_suite
from .config import (
    _BULK_AT_CONTENT_MAXLEN,
    COMMENT_DISTRIBUTION,
    LIKE_DISTRIBUTION,
    VIEW_DISTRIBUTION,
)


async def _seed_dynamic(
    client: SeedClient,
    user_pool: list[tuple[int, str | None]],
    topic_ids: list[int],
    real: tuple,
    sem: asyncio.Semaphore,
    author_cycle: "cycle[tuple[int, str | None]]",
) -> None:
    """单条动态：创建 → 审核通过 → 按分布点赞/评论/@/浏览（并发，全定值轮遍）。

    作者在 ``user_pool`` 上**轮流循环**（round-robin，``author_cycle``），
    保证每个用户都被轮到创建动态，总次数 = ``len(reals)``（= ``count``）。
    其余互动者（点赞 / 浏览 / @ / 评论）亦确定性轮遍（``_rr``），不再随机。

    正文末尾追加轮遍 @（不 @ 作者本人），让灌数数据同样覆盖动态 @ 链路；
    评论走 ``_seed_bulk_comment_suite``（一级评论 + 楼中楼 + 评论赞踩 + 显式 @ +
    举报 + 置顶，正文末尾轮遍 @），覆盖评论 @ 链路并触发 REPLY（给动态作者）/
    AT（给被 @ 用户）事件通知，使「收到的赞/回复/@」消息中心有真实数据；
    极长正文（已接近 2000 字上限）跳过 @，避免触发长度校验导致整条动态灌入失败。
    """
    async with sem:
        try:
            _dyn_id, _pub_time, content, _comment_count, _repost_count = real
            author, _ = next(author_cycle)
            nodes: list[dict] = [{"type": "WORDS", "text": content}]
            if len(content) <= _BULK_AT_CONTENT_MAXLEN:
                nodes.extend(_at_nodes(_at_targets_deterministic(user_pool, author)))
            # 话题：轮遍挂载（非概率）
            topic_id = _rr.pick(topic_ids) if topic_ids and _rr.pick(range(2)) == 0 else None
            new_dyn_id = await client.create_dynamic(
                author, scene="WORD", content=nodes, topic_id=topic_id
            )
            await client.approve(new_dyn_id)

            like_target = _sample(LIKE_DISTRIBUTION)
            view_target = _sample(VIEW_DISTRIBUTION)
            comment_target = _sample(COMMENT_DISTRIBUTION)
            tasks = []
            if like_target > 0 and user_pool:
                for u in _rr.pick_n(user_pool, min(like_target, len(user_pool))):
                    tasks.append(client.thumb(u[0], new_dyn_id))
            if view_target > 0 and user_pool:
                for u in _rr.pick_n(user_pool, min(view_target, len(user_pool))):
                    tasks.append(client.browse(u[0], new_dyn_id))

            # 评论套件：对动态跑完整评论链路（一级评论/@ + 楼中楼 + 评论赞踩 +
            # 显式 @ + 举报 + 置顶），生成 REPLY（动态作者）/ AT（被 @ 用户）事件，
            # 使「收到的赞/回复/@」消息中心有真实数据；单条失败不影响整体。
            # comment_target 为 0（多数动态无评论）时跳过整段评论，与分布一致。
            if comment_target > 0 and user_pool:
                tasks.append(
                    _seed_bulk_comment_suite(
                        client,
                        user_pool,
                        new_dyn_id,
                        author,  # up_mid：动态作者，接收 REPLY 事件
                        InteractionBizTypeEnum.DYNAMIC,
                    )
                )

            if tasks:
                # 单条动态的点赞/评论/@/浏览并发发起，单条失败不影响整体
                await asyncio.gather(*tasks, return_exceptions=True)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"动态灌入失败（跳过）: {e}")
