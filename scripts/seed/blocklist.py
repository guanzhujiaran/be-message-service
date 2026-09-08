"""黑名单归一与私信配对避让。

黑名单持久化且跨 seed 累积：每多一条，就多一个用户被静默排除在评论 / 私信 /
关注 / @ 通知之外（服务端静默拒绝，seed 只能软降级跳过，覆盖率逐轮变窄）。
故归一到「每人 ≤1 条」（够验证负面场景即可），其余走 ``POST /message/follow/unblock`` 解除。
"""
from loguru import logger
from sqlmodel import col, select

from app.core.database import new_session
from app.models.db.follow_tbl import UserFollow
from app.models.enums import FollowStatusEnum

from .client import SeedClient


async def _load_block_relations(mids: list[int]) -> list[tuple[int, int]]:
    """只读探针：批量取这些用户**主动拉黑**的关系 ``[(mid, target_mid)]``。

    黑名单持久化且跨 seed 累积：每多一条，就多一个用户被静默排除在评论 /
    私信 / 关注 / @ 通知之外（服务端静默拒绝，seed 只能软降级跳过，
    互动覆盖逐轮变窄）。

    返回**有序**列表（同一 mid 内按 ``created_at`` 倒序、``target_mid`` 兜底），
    供 ``_normalize_blocklist`` 直接按序保留最新的几条。
    """
    if not mids:
        return []
    async with new_session() as s:
        rows = (
            await s.exec(
                select(UserFollow.mid, UserFollow.target_mid)
                .where(
                    col(UserFollow.mid).in_(mids),
                    col(UserFollow.status) == FollowStatusEnum.BLOCKED,
                )
                .order_by(
                    col(UserFollow.mid),
                    col(UserFollow.created_at).desc(),
                    col(UserFollow.target_mid),
                )
            )
        ).all()
    return [(int(m), int(t)) for m, t in rows]


def _is_dm_blocked(
    blocked: set[tuple[int, int]], sender_mid: int, receiver_mid: int
) -> bool:
    """``sender_mid`` 发给 ``receiver_mid`` 的私信是否会被黑名单拒绝。

    对齐服务端 ``FollowService.is_blocked_by(sender_mid, receiver_mid)`` 的
    **单向**判定：只有「接收方拉黑了发送方」才拒绝发送；反向拉黑不阻断本方向。
    """
    return (int(receiver_mid), int(sender_mid)) in blocked


async def _normalize_blocklist(
    client: SeedClient,
    users: list[tuple[int, str | None]],
    *,
    keep: int = 1,
) -> set[tuple[int, int]]:
    """把每个用户的主动黑名单收敛到最多 ``keep`` 条，其余走 unblock 接口解除。

    保留 ``keep`` 条用于验证「拉黑后不可互动」的负面场景，其余历史累积的黑名单
    会持续吃掉评论 / 私信 / 关注 / @ 通知的覆盖率，必须清理。
    **按 created_at 倒序保留最新的**——本次 seed 刚演示拉黑的那条必须留下，
    被清掉的应是更早的历史遗留。读走只读探针、写走业务接口
    ``POST /message/follow/unblock``，不直写主库。

    Returns:
        归一后**保留**的黑名单关系集合，供私信场景避让。
    """
    mids = [int(u[0]) for u in users]
    rows = await _load_block_relations(mids)
    if not rows:
        return set()
    kept: set[tuple[int, int]] = set()
    stale: list[tuple[int, int]] = []
    quota: dict[int, int] = {}
    for m, t in rows:
        if quota.get(m, 0) < keep:
            quota[m] = quota.get(m, 0) + 1
            kept.add((m, t))
        else:
            stale.append((m, t))
    for m, t in stale:
        try:
            await client.unblock(m, t)
        except RuntimeError as e:
            logger.warning(f"清理历史黑名单 {m}→{t} 失败（已跳过）: {e}")
    if stale:
        logger.success(
            f"[黑名单归一] 解除 {len(stale)} 条历史拉黑，每人保留 ≤{keep} 条（保留 {len(kept)} 条）"
        )
    else:
        logger.info(f"[黑名单归一] 无需清理，已有 {len(kept)} 条黑名单（每人 ≤{keep} 条）")
    return kept
