"""大数据灌数：单个资源（动态 / lottery）的完整评论套件（软降级，单条失败不阻断）。

覆盖：资源点赞（按 biz_type 分流）、一级评论（正文末尾轮遍 @）、楼中楼、评论赞踩、
显式 @、举报、置顶；lottery 额外补发 LIKE 事件（通用资源点赞后端不自动生成）。
"""
import random

from bili_common.models import InteractionActionTypeEnum, InteractionBizTypeEnum
from loguru import logger

from ..client import SeedClient
from ..config import _TOP_COMMENT_PROB
from ..material import _REPLIES
from ..rr import _rr


async def _seed_bulk_comment_suite(
    client: SeedClient,
    user_pool: list[tuple[int, str | None]],
    oid: int,
    up_mid: int,
    biz_type: InteractionBizTypeEnum,
    *,
    like_event_recipient: int | None = None,
) -> None:
    """大数据灌数：单个资源（动态 / lottery）的完整评论套件（软降级，单条失败不阻断）。

    覆盖：资源点赞（按 biz_type 分流 DYNAMIC→thumb / LOTTERY→thumb_lottery）、
    一级评论（正文末尾轮遍 @）、楼中楼（二级评论，带 @）、评论点赞/点踩、
    显式 @ 评论、举报、置顶；lottery 资源额外补发 LIKE 事件（通用资源点赞后端不自动
    生成事件，与 ``seed_lottery_resource`` 行为一致），统一落点 ``like_event_recipient``
    便于登录消息中心查看「收到的赞」。全流程**定值轮遍**（不再随机）。
    """
    if len(user_pool) < 2:
        return
    if up_mid and any(u[0] == up_mid for u in user_pool):
        commenters = [u for u in user_pool if u[0] != up_mid]
    else:
        # up_mid 不在用户池（lottery 等无作者资源）时轮遍指派一个资源作者
        up_mid = _rr.pick(user_pool)[0]
        commenters = [u for u in user_pool if u[0] != up_mid]
    if not commenters:
        return

    async def _safe(coro) -> None:
        """单条评论动作失败软降级跳过。"""
        try:
            await coro
        except Exception as e:  # noqa: BLE001
            logger.warning(f"灌数评论动作失败（已跳过）: {e}")

    async def _safe_add(coro):
        """评论发布失败软降级，返回 rpid 或 None。"""
        try:
            return await coro
        except Exception as e:  # noqa: BLE001
            logger.warning(f"灌数评论发布失败（已跳过）: {e}")
            return None

    def _rr_at(exclude_mid: int | None) -> list[tuple[int, str]]:
        """确定性轮遍 @ 目标：从 commenters（排除自己）轮流取 1 个。"""
        pool = [u for u in commenters if u[0] != exclude_mid]
        if not pool:
            return []
        mid, name = _rr.pick(pool)
        return [(int(mid), (name or f"user{mid}"))]

    # 0) 资源点赞（按 biz_type 分流）
    liker = _rr.pick(commenters)
    if biz_type is InteractionBizTypeEnum.LOTTERY:
        await _safe(client.thumb_lottery(liker[0], oid))
    else:
        await _safe(client.thumb(liker[0], oid))

    # 1) 一级评论（固定 2 条，轮遍取评论者）+ 审核通过（正文末尾轮遍 @，不 @ 自己）
    root_rpids: list[str] = []
    for commenter in _rr.pick_n(commenters, min(2, len(commenters))):
        rpid = await _safe_add(
            client.add_comment(
                commenter[0],
                oid,
                up_mid,
                biz_type=biz_type,
                at_users=_rr_at(commenter[0]),
            )
        )
        if not rpid:
            continue
        await _safe(client.approve_comment(rpid))
        root_rpids.append(rpid)

    # 2) 楼中楼（二级评论）：对每根一级评论固定回复 1 层，正文末尾轮遍 @
    for root_rpid in root_rpids:
        parent = root_rpid
        replier = _rr.pick(commenters)
        rpid = await _safe_add(
            client.add_comment(
                replier[0],
                oid,
                up_mid,
                biz_type=biz_type,
                root=root_rpid,
                parent=parent,
                message=_rr.pick(_REPLIES),
                at_users=_rr_at(replier[0]),
            )
        )
        if not rpid:
            continue
        await _safe(client.approve_comment(rpid))
        parent = rpid

    # 3) 评论点赞 / 点踩（落在一级评论上，赞/踩交替）
    for i, rpid in enumerate(root_rpids):
        actor = _rr.pick(commenters)
        await _safe(client.comment_action(actor[0], rpid, 1 if i % 2 == 0 else 2))

    # 4) 显式 @ 评论（@ + 末尾轮遍 @ 共存）
    if len(commenters) >= 2:
        at_target = commenters[0]
        at_name = at_target[1] or f"user{at_target[0]}"
        subject = "抽奖" if biz_type is InteractionBizTypeEnum.LOTTERY else "动态"
        await _safe(
            client.add_comment(
                commenters[1][0],
                oid,
                up_mid,
                biz_type=biz_type,
                message=f"@{at_name} 这个{subject}真不错",
                at_mids=[at_target[0]],
                at_name_to_mid={at_name: at_target[0]},
                at_users=_rr_at(commenters[1][0]),
            )
        )

    # 5) 举报一条评论
    if root_rpids:
        await _safe(client.report_comment(_rr.pick(commenters)[0], root_rpids[-1]))

    # 6) 评论置顶（资源作者身份）。置顶为覆盖验证型动作，按概率抽样
    #    （_TOP_COMMENT_PROB），不逐资源必测——大数据灌数 count 大时避免上千次置顶。
    if root_rpids and random.random() < _TOP_COMMENT_PROB:
        await _safe(client.top_comment(up_mid, oid, root_rpids[-1], biz_type=biz_type))

    # lottery 资源点赞后端不自动生成 LIKE 事件 → 补发（与 seed_lottery_resource 一致）
    if biz_type is InteractionBizTypeEnum.LOTTERY and like_event_recipient is not None:
        actor = _rr.pick(commenters)
        await _safe(
            client.report_event(
                like_event_recipient,
                InteractionActionTypeEnum.LIKE,
                oid,
                actor[0],
                actor[1],
                str(oid),
                biz_type=InteractionBizTypeEnum.LOTTERY,
            )
        )
