"""场景② 评论体系：一级评论 + 楼中楼 + 赞踩 + @ + 举报 + 置顶。

在**动态与 lottery 混合**资源池 ``[(oid, biz_type)]`` 上跑同一套互动，
保证通用资源链路与动态链路覆盖对等（不出现「评论只覆盖动态」的偏斜）。
"""
import asyncio
import random

from bili_common.models import InteractionBizTypeEnum
from loguru import logger
from tqdm import tqdm

from ..client import SeedClient
from ..config import _TOP_COMMENT_PROB
from ..material import _REPLIES
from ..rr import _rr
from ..verify import _verify_comment_at


async def seed_comment(
    client: SeedClient,
    users: list[tuple[int, str | None]],
    resources: list[tuple[int, InteractionBizTypeEnum]],
) -> None:
    """② 评论体系：一级评论 + 楼中楼 + 点赞/点踩 + @（每条正文末尾随机 @）+ 举报 + 置顶。

    ``resources`` 是**动态与 lottery 混合**的资源池 ``[(oid, biz_type)]``：
    同一套互动在两种 biz_type 上各跑一遍，保证通用资源链路与动态链路覆盖对等，
    不会出现「评论只覆盖动态、点赞只覆盖抽奖」的偏斜。
    """
    if not resources:
        logger.warning("无可用资源（动态与 lottery 均为空），跳过评论体系。")
        return

    root_rpids: list[str] = []
    comment_count = 0
    action_count = 0
    like_count = 0

    for oid, biz_type in tqdm(resources, desc="[评论体系] 资源", unit="个"):
        # 评论区 up_mid 用资源作者（动态从用户池轮遍取）
        up_mid = _rr.pick(users)[0]
        commenters = [u for u in users if u[0] != up_mid]
        # 仅当前资源的根评论（楼中楼/点赞/置顶必须限定在同一个评论区）
        dyn_root_rpids: list[str] = []

        def _rr_at_targets(exclude_mid: int | None) -> list[tuple[int, str]]:
            """确定性轮遍 @ 目标：从 commenters（排除自己）轮流取 1 个。"""
            pool = [u for u in commenters if u[0] != exclude_mid]
            if not pool:
                return []
            mid, name = _rr.pick(pool)
            return [(int(mid), (name or f"user{mid}"))]

        # 0) 资源点赞：动态走 thumb，lottery 等通用资源走 thumb_lottery
        #    （通用资源点赞后端不自动生成 LIKE 事件，消息中心覆盖由
        #     seed_lottery_resource 显式补发，此处不重复补）
        for u in _rr.pick_n(commenters, min(2, len(commenters))):
            if biz_type is InteractionBizTypeEnum.LOTTERY:
                await client.thumb_lottery(u[0], oid)
            else:
                await client.thumb(u[0], oid)
            like_count += 1

        # 1) 一级评论（固定 2 条，轮遍取评论者）+ 审核通过（正文末尾轮遍 @，不 @ 自己）
        for commenter in _rr.pick_n(commenters, min(2, len(commenters))):
            try:
                rpid = await client.add_comment(
                    commenter[0],
                    oid,
                    up_mid,
                    biz_type=biz_type,
                    at_users=_rr_at_targets(commenter[0]),
                )
                await client.approve_comment(rpid)
            except RuntimeError as e:
                # 拉黑等业务限制会拒绝评论（seed 组合可能命中持久化黑名单），软降级跳过
                logger.warning(f"评论发布失败（已跳过）: {e}")
                continue
            root_rpids.append(rpid)
            dyn_root_rpids.append(rpid)
            comment_count += 1
            await asyncio.sleep(0.02)

        # 2) 楼中楼：对当前资源每个根评论固定回复 1 层（轮遍取回复者）
        for root_rpid in dyn_root_rpids:
            parent = root_rpid
            replier = _rr.pick(commenters)
            try:
                rpid = await client.add_comment(
                    replier[0],
                    oid,
                    up_mid,
                    biz_type=biz_type,
                    root=root_rpid,
                    parent=parent,
                    message=_rr.pick(_REPLIES),
                    at_users=_rr_at_targets(replier[0]),
                )
                await client.approve_comment(rpid)
            except RuntimeError as e:
                # 同上：评论被业务拒绝（拉黑等）软降级跳过，不阻断楼中楼后续
                logger.warning(f"楼中楼评论发布失败（已跳过）: {e}")
                continue
            comment_count += 1
            parent = rpid
            await asyncio.sleep(0.02)

        # 3) 评论点赞 / 点踩（轮遍取动作者，赞/踩交替）
        for i, rpid in enumerate(dyn_root_rpids):
            actor = _rr.pick(commenters)
            await client.comment_action(actor[0], rpid, 1 if i % 2 == 0 else 2)
            action_count += 1

        # 4) @ 提及：一级评论带 at_mids + at_name_to_mid（显式 @ + 末尾轮遍 @ 并存）
        if len(commenters) >= 2:
            at_target = commenters[0]
            at_name = at_target[1] or f"user{at_target[0]}"
            subject = "抽奖" if biz_type is InteractionBizTypeEnum.LOTTERY else "动态"
            try:
                await client.add_comment(
                    commenters[1][0],
                    oid,
                    up_mid,
                    biz_type=biz_type,
                    message=f"@{at_name} 这个{subject}真不错",
                    at_mids=[at_target[0]],
                    at_name_to_mid={at_name: at_target[0]},
                    at_users=_rr_at_targets(commenters[1][0]),
                )
                comment_count += 1
            except RuntimeError as e:
                # 同上：@ 评论被业务拒绝（拉黑等）软降级跳过
                logger.warning(f"@ 评论发布失败（已跳过）: {e}")

        # 5) 举报当前资源的一条评论
        if dyn_root_rpids:
            await client.report_comment(_rr.pick(commenters)[0], dyn_root_rpids[-1])

        # 6) 评论置顶（资源作者身份，仅置顶当前资源的根评论）。
        #    置顶为覆盖验证型动作，按概率抽样（_TOP_COMMENT_PROB），不逐资源必测。
        if dyn_root_rpids and random.random() < _TOP_COMMENT_PROB:
            try:
                await client.top_comment(
                    up_mid, oid, dyn_root_rpids[-1], biz_type=biz_type
                )
            except RuntimeError as e:
                logger.warning(f"评论置顶失败: {e}")

    # 7) @ 验证：每种 biz_type 各回查一个资源，确认随机 @ 已落库 + 渲染 + 触发 AT 通知
    verified: set[InteractionBizTypeEnum] = set()
    for oid, biz_type in resources:
        if biz_type in verified:
            continue
        verified.add(biz_type)
        await _verify_comment_at(client, users[0][0], oid, biz_type=biz_type)

    dyn_n = sum(1 for _, t in resources if t is InteractionBizTypeEnum.DYNAMIC)
    logger.success(
        f"[评论体系] 资源 {len(resources)} 个（动态 {dyn_n} / lottery "
        f"{len(resources) - dyn_n}）：评论 {comment_count} 条（含楼中楼），"
        f"资源点赞 {like_count} 次，评论互动 {action_count} 次，@/举报/置顶已覆盖"
    )
