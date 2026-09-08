"""阶段二编排：基于 biliopusdb 真实动态的大数据灌数（走 API，软降级）。

流程：拉真实动态/话题 → 建话题（幂等）→ 灌动态（点赞/浏览/评论套件）→
lottery 评论套件 → pptr 用户池批量互发私信。
"""
import argparse
import asyncio
import sys
from itertools import cycle

from bili_common.models import InteractionBizTypeEnum
from loguru import logger
from tqdm import tqdm

from ..client import SeedClient
from ..config import _LOTTERY_POOL_SIZE, _SEED_REQ_CONCURRENCY
from ..datasource import (
    _load_existing_topics,
    fetch_pptr_user_pool,
    fetch_real_dyns,
    fetch_real_topics,
)
from ..lottery import _fetch_lottery_ids, _fetch_lottery_ids_via_api
from ..material import _load_material_pools
from ..rr import _rr, _sample
from .comment import _seed_bulk_comment_suite
from .config import COMMENT_DISTRIBUTION, LIKE_DISTRIBUTION, VIEW_DISTRIBUTION
from .dm import _seed_bulk_dm
from .dynamic import _seed_dynamic
from .topics import _seed_topics


async def run_bulk(args: argparse.Namespace) -> None:
    # 素材池真实化：从 biliopusdb / bilidb 拉取真实素材（失败降级内置兜底）
    await _load_material_pools()
    logger.info(
        f"灌数计划（走 API）：count={args.count}, base_url={args.base_url}, "
        f"concurrency={args.concurrency}, dry_run={args.dry_run}"
    )
    if args.count < 1:
        logger.error("count 必须 >= 1")
        sys.exit(2)

    # 1. 拉取数据源（只读 biliopusdb / pptr，不写主库）
    logger.info("拉取 biliopusdb 真实数据…")
    reals = await fetch_real_dyns(args.count)
    if not reals:
        logger.error("biliopusdb 无有效数据，中止")
        sys.exit(1)
    topic_names = await fetch_real_topics()
    logger.info(f"真实动态 {len(reals)} 条 / 真实话题 {len(topic_names)} 个")

    if args.dry_run:
        like_total = sum(_sample(LIKE_DISTRIBUTION) for _ in reals)
        view_total = sum(_sample(VIEW_DISTRIBUTION) for _ in reals)
        comment_total = sum(_sample(COMMENT_DISTRIBUTION) for _ in reals)
        logger.info(
            f"[dry-run] 将经 API 灌入：动态 {len(reals)}、话题 {len(topic_names)}、"
            f"点赞约 {like_total}、一级评论/@约 {comment_total}（每条再附带楼中楼/赞踩/"
            f"举报/置顶，触发 REPLY/AT 事件）、浏览约 {view_total}；"
            f"另取 {_LOTTERY_POOL_SIZE} 个真实 lottery_id 跑等价评论套件（含资源点赞补 LIKE 事件）；"
            f"不调用接口"
        )
        return

    # 作者 / 点赞者 / 浏览者 / @对象统一取自自有用户系统（pptr Postgres）。
    user_pool = await fetch_pptr_user_pool(args.users_pool_size)
    if not user_pool:
        logger.error("pptr 无可用用户 uid，无法映射动态作者，中止")
        sys.exit(1)
    logger.info(f"自有用户池（pptr）{len(user_pool)} 个，用作作者/点赞者/浏览者/@对象")

    async with SeedClient(
        args.base_url, args.admin_mid, req_concurrency=_SEED_REQ_CONCURRENCY
    ) as client:
        # 2. 话题：预拉已有话题（直读主库幂等），并发创建 + 审核通过
        logger.info("预拉已有话题（直读主库，幂等）…")
        existing_topics = await _load_existing_topics()
        logger.info(f"  已有话题 {len(existing_topics)} 个")
        logger.info("创建并审核话题…")
        topic_ids = await _seed_topics(
            client, topic_names, args.admin_mid, args.concurrency, existing_topics
        )
        logger.info(f"  话题 {len(topic_ids)} 个（含复用已有）")

        # 3. 动态：并发创建 + 审核 + 点赞/浏览（正文末尾随机 @）
        # 作者在 user_pool 上轮流循环（round-robin），每个用户都轮到创建，总次数 = len(reals)
        logger.info("经 API 灌入动态（含点赞/浏览/@）…")
        sem = asyncio.Semaphore(args.concurrency)
        author_cycle = cycle(user_pool)
        tasks = [
            asyncio.create_task(
                _seed_dynamic(
                    client,
                    user_pool,
                    topic_ids,
                    real,
                    sem,
                    author_cycle,
                )
            )
            for real in reals
        ]
        for f in tqdm(
            asyncio.as_completed(tasks), total=len(tasks), desc="seed moments"
        ):
            try:
                await f
            except Exception as e:  # noqa: BLE001
                logger.warning(f"单条动态任务异常（已跳过）: {e}")

        # 4. lottery 资源：取真实 lottery_id，跑与动态等价的完整评论套件
        #    （一级评论/@ + 楼中楼 + 评论赞踩 + 显式 @ + 举报 + 置顶 + 资源点赞
        #    补 LIKE 事件），补齐「大数据灌数」阶段对通用资源评论链路的覆盖。
        logger.info("经 API 灌入 lottery 资源评论套件…")
        lottery_ids = await _fetch_lottery_ids_via_api(
            args.crawler_base_url, _LOTTERY_POOL_SIZE
        )
        if not lottery_ids:
            lottery_ids = await _fetch_lottery_ids(_LOTTERY_POOL_SIZE)
        if lottery_ids and user_pool:
            like_recipient = user_pool[0][0]  # LIKE 事件统一落点（可登录查看消息中心）
            lot_tasks = [
                _seed_bulk_comment_suite(
                    client,
                    user_pool,
                    int(lid),
                    _rr.pick(user_pool)[0],  # lottery 无作者，轮遍指派资源作者
                    InteractionBizTypeEnum.LOTTERY,
                    like_event_recipient=like_recipient,
                )
                for lid in lottery_ids
            ]
            for f in tqdm(
                asyncio.as_completed(lot_tasks),
                total=len(lot_tasks),
                desc="seed lottery",
                unit="个",
            ):
                try:
                    await f
                except Exception as e:  # noqa: BLE001
                    logger.warning(f"lottery 资源灌数失败（已跳过）: {e}")
            logger.info(
                f"  lottery 资源 {len(lottery_ids)} 个评论套件完成"
                f"（点赞 + LIKE 事件→{like_recipient} 已覆盖）"
            )
        else:
            logger.warning(
                "无可用 lottery_id（接口与直连库均失败），跳过 lottery 资源灌数"
            )

        # 5. 私信：在 pptr 用户池上批量互发，填充 DM 内容（含 127763472 等任意 pptr 用户）。
        #    全互动阶段仅在随机 12 人子集上跑私信，大数据灌数阶段用完整 pptr 池补覆盖，
        #    使登录 demo 的测试账号也能看到私信内容（发送方自己的消息始终可见）。
        if user_pool:
            logger.info("经 API 灌入私信（pptr 用户池批量互发）…")
            await _seed_bulk_dm(
                client,
                user_pool,
                args.count,
                args.concurrency,
            )
        else:
            logger.warning("无可用用户池，跳过大数据灌数私信")

    logger.info("灌数完成！浏览计数由热路径原子 ±1 维护（2.42.0 起对账脚本已移除，浏览明细每用户每资源一行）。")
