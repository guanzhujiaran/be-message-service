"""场景① 动态体系：话题创建/审核 + 发布/审核 + 点赞 + 转发 + 浏览 + 举报 + 置顶。

转发覆盖三条链路：``/repost``（源池含已过审转发 → 二级转发，``repostDepth`` 递增）、
``POST /create``(scene=FORWARD) 独立分支（支持挂话题）、以及转发语 @（详情回显 +
AT 事件断言）与源动态 ``repostCount +1`` 状态机校验。
"""
import asyncio
from itertools import cycle

from loguru import logger
from tqdm import tqdm

from ..client import SeedClient
from ..helpers import _content_nodes
from ..material import _SENTENCES, _TOPIC_NAMES
from ..rr import _rr
from ..verify import _verify_dynamic_at, _verify_repost_count


async def seed_moment(
    client: SeedClient,
    users: list[tuple[int, str | None]],
    count: int,
    concurrency: int,
) -> list[int]:
    """① 动态体系：话题 + 发布/审核 + 点赞 + 转发 + 浏览 + 举报 + 置顶。

    动态创建按 ``concurrency`` 并发执行（``asyncio.Semaphore`` 限流），
    单条失败软降级跳过，不影响整体进度；转发池 ``normal_ids``/``forward_ids``
    等共享状态在单事件循环内由主协程聚合，无竞态。

    转发覆盖三条链路：步骤 6) 的 ``/repost``（源池含已过审转发 → 二级转发，
    ``repostDepth`` 递增）、步骤 9) 的 ``POST /create``(scene=FORWARD) 独立分支
    （支持挂话题），以及转发语 @ 落库/渲染/AT 事件与源动态 ``repostCount +1`` 校验。
    """
    # 作者在全部用户上轮流循环（round-robin），保证每个用户都被轮到创建动态，
    # 总次数 = count（不再只用前半用户 / 随机选）。
    author_cycle = cycle(users)
    normal_ids: list[int] = []
    forward_ids: list[int] = []
    like_count = 0
    topic_ids: list[int] = []

    # 建话题 + 审核通过（话题名全局唯一；话题名轮遍 _TOPIC_NAMES，全局唯一用递增序号后缀，
    # 不再用 uuid4 随机后缀——序号由 _rr 游标保证不重复）。
    creator_mid, _ = next(author_cycle)
    topic_seq = 0
    for _ in range(2):
        topic_seq += 1
        name = f"{_rr.pick(_TOPIC_NAMES)}_{topic_seq}"
        try:
            tid = await client.create_topic(creator_mid, name)
            await client.approve_topic(tid)
            topic_ids.append(tid)
        except RuntimeError as e:
            logger.warning(f"话题创建/审核失败: {e}")
    logger.info(f"已建并过审话题 {len(topic_ids)} 个")

    sem = asyncio.Semaphore(concurrency)

    def _rr_at_targets(exclude_mid: int | None) -> list[tuple[int, str]]:
        """确定性轮遍 @ 目标：从 users 里（排除自己）轮流取 1 个。"""
        pool = [u for u in users if u[0] != exclude_mid]
        if not pool:
            return []
        mid, name = _rr.pick(pool)
        return [(int(mid), (name or f"user{mid}"))]

    async def _one() -> tuple[int | None, int | None, int | None, int | None, int]:
        """创建一条动态并完成 发布/审核/点赞/浏览/举报/转发（全定值轮遍）。

        返回 ``(作者, 动态, 转发动态, 转发源动态, 点赞数)``。
        """
        async with sem:
            author_mid, _ = next(author_cycle)
            sentence = _rr.pick(_SENTENCES)
            # 正文末尾追加 @（不 @ 自己），覆盖动态 @ 落库 + 事件通知链路
            at_targets = _rr_at_targets(author_mid)
            # 话题：轮流挂载（topic_ids 非空时每 2 条带 1 个话题，避免全部/全不带）
            topic_id = _rr.pick(topic_ids) if (topic_ids and _rr.pick(range(2)) == 0) else None

            # 1) 发布（正文图轮遍 _IMG_URLS，非随机 30%）
            content = _content_nodes(sentence, at_targets, with_image=_rr.pick(range(10)) == 0)
            dyn_id = await client.create_dynamic(
                author_mid,
                scene="WORD",
                content=content,
                topic_id=topic_id,
            )
            # 2) 审核通过 → normal
            await client.approve(dyn_id)

            # 3) 点赞：除作者外轮流取 3 个点赞（用户不足则全点）
            likers = [u for u in users if u[0] != author_mid]
            n_like = 0
            for liker in _rr.pick_n(likers, min(3, len(likers))):
                await client.thumb(liker[0], dyn_id)
                n_like += 1

            # 4) 浏览（detail 触发浏览 MQ）：轮遍取 1 个非作者
            if likers:
                viewer = _rr.pick(likers)[0]
                await client.browse(viewer, dyn_id)

            # 5) 举报：每 10 条动态举报 1 次（确定性，不再概率随机）
            if likers and _rr.pick(range(10)) == 0:
                await client.report_moment(_rr.pick(likers)[0], dyn_id)

            # 6) 转发：对创建好的动态尝试转发——每创建一条后，若已有过审动态，
            #    就从已过审集合里轮遍取一条作为源进行转发（覆盖转发链路），不再概率触发。
            #    源池含已过审的转发动态 → 覆盖二级转发（FORWARD→FORWARD，repostDepth 递增）。
            fwd_id: int | None = None
            fwd_src: int | None = None
            src_pool = normal_ids + forward_ids
            if src_pool:
                src_dyn = _rr.pick(src_pool)
                fwd_id = await client.repost(
                    author_mid,
                    src_dyn,
                    _content_nodes(
                        "转发：这个说得太对了",
                        _rr_at_targets(author_mid),
                        with_image=False,
                    ),
                )
                await client.approve(fwd_id)
                fwd_src = src_dyn

            return author_mid, dyn_id, fwd_id, fwd_src, n_like

    # 并发创建（asyncio.Semaphore 限流），tqdm 实时进度
    tasks = [asyncio.create_task(_one()) for _ in range(count)]
    first_dyn_owner: int | None = None
    # (源动态, 转发动态) 对，用于回查源动态 repostCount 是否 +1
    fwd_pairs: list[tuple[int, int]] = []
    # 记录失败原因样本：失败本是共性根因（HTTP/业务错误），单条 warning 会被 ERROR 过滤掉，
    # 若一条动态都没成功，必须把代表性失败原因以 error 打出来，否则无法定位（"0条成功"只是现象）。
    _fail_samples: list[str] = []
    for f in tqdm(
        asyncio.as_completed(tasks), total=count, desc="[moment] 动态", unit="条"
    ):
        try:
            author_mid, dyn_id, fwd_id, fwd_src, n_like = await f
            normal_ids.append(dyn_id)
            if fwd_id is not None and fwd_src is not None:
                forward_ids.append(fwd_id)
                fwd_pairs.append((fwd_src, fwd_id))
            like_count += n_like
            if first_dyn_owner is None:
                first_dyn_owner = author_mid
        except RuntimeError as e:
            if len(_fail_samples) < 5:
                _fail_samples.append(str(e))
            logger.warning(f"单条动态任务异常（已跳过）: {e}")

    if normal_ids:
        # 部分成功但存在失败样本：提醒（不属于 ERROR，仅个别抖动）
        if _fail_samples:
            logger.warning(
                f"[动态] 成功 {len(normal_ids)} 条，失败 {len(_fail_samples)}+ 条样本: "
                f"{_fail_samples[0]}"
            )
    else:
        # 动态 0 条成功 = 链路级故障（共性根因），必须响亮打印代表性失败原因
        logger.error(
            f"[动态] 动态创建 0 条成功（共 {count} 条全部失败）。代表性失败原因: {_fail_samples}"
        )

    # 7) 置顶第一条动态（作者本人 + normal）
    if normal_ids and first_dyn_owner is not None:
        try:
            await client.top_moment(first_dyn_owner, normal_ids[0])
        except RuntimeError as e:
            logger.warning(f"置顶失败（动态非本人/non-normal）: {e}")

    # 8) @ 验证：回查详情，确认正文末尾随机 @ 已落库 + 渲染 + 触发 AT 通知
    if normal_ids:
        await _verify_dynamic_at(client, users[0][0], normal_ids[0])

    # 9) 转发体系：`/repost` 已在步骤 6) 覆盖，此处补 `POST /create`(scene=FORWARD) 分支
    #    （两条独立实现，create 分支额外支持挂话题）+ 转发语 @ + 源动态转发计数校验。
    if normal_ids:
        cf_mid = users[0][0]
        try:
            cf_id = await client.create_forward(
                cf_mid,
                normal_ids[0],
                _content_nodes(
                    "转发(create FORWARD)：带话题的转发",
                    _rr_at_targets(cf_mid),
                ),
                topic_id=_rr.pick(topic_ids) if topic_ids else None,
            )
            await client.approve(cf_id)
            forward_ids.append(cf_id)
            fwd_pairs.append((normal_ids[0], cf_id))
            logger.success(f"[转发] create(scene=FORWARD) 链路通过 dyn={cf_id}")
        except RuntimeError as e:
            logger.error(f"[转发] create(scene=FORWARD) 失败: {e}")

    if forward_ids:
        # 转发语 @：详情回显 + 渲染 + AT 事件（与 WORD 动态同一套断言）
        await _verify_dynamic_at(client, users[0][0], forward_ids[0])
        # 源动态 repostCount +1（转发过审时的状态机触发点）
        await _verify_repost_count(client, users[0][0], fwd_pairs)
    else:
        logger.error("[转发] 未产出任何转发动态，转发链路未覆盖")

    logger.success(
        f"[动态体系] 动态 {len(normal_ids)} 条（含转发 {len(forward_ids)}），点赞 {like_count} 次，话题 {len(topic_ids)} 个"
    )
    return normal_ids
