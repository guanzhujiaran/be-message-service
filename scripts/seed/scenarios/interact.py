"""场景③ 用户级互动：收藏夹（含封面审核）+ 收藏 + 关注/拉黑 + 关注流 + 事件/系统通知。"""
from bili_common.models import InteractionActionTypeEnum
from loguru import logger
from tqdm import tqdm

from ..client import SeedClient
from ..material import _IMG_URLS
from ..rr import _rr


async def seed_interact(
    client: SeedClient,
    users: list[tuple[int, str | None]],
    normal_ids: list[int],
    *,
    skip_follow: bool = False,
) -> None:
    """③ 用户级互动：收藏夹 + 关注/拉黑 + 关注流验证 + 事件通知 + 系统通知。"""
    if not normal_ids:
        logger.warning("无可用动态，跳过用户级互动。")
        return

    folder_ids: list[str] = []
    # 1) 收藏夹：每人建一个（轮流带封面 → 封面审核）
    for i, (mid, _) in enumerate(users[: min(3, len(users))]):
        name = f"seed收藏夹{mid}"
        cover = _rr.pick(_IMG_URLS) if i % 2 == 0 else None
        fid = await client.create_folder(mid, name, cover)
        if fid:
            folder_ids.append(fid)
            await client.approve_folder_cover(fid)
    # 默认收藏夹（不传 folderId 走默认夹）
    if users:
        await client.favorite_setting(users[0][0], True)

    # 2) 收藏动态（每人收藏 1~2 条到自己的收藏夹）
    fav_count = 0
    for i, (mid, _) in tqdm(
        enumerate(users[: min(5, len(users))]), desc="[用户级互动] 收藏", unit="人"
    ):
        fid = folder_ids[i % len(folder_ids)] if folder_ids else None
        for dyn_id in _rr.pick_n(normal_ids, min(2, len(normal_ids))):
            try:
                if fid:
                    await client.favorite_add(mid, dyn_id, fid)
                else:
                    await client.favorite_add(mid, dyn_id, "")
                fav_count += 1
            except RuntimeError as e:
                logger.warning(f"收藏失败: {e}")

    # 3) 关注 / 拉黑
    hub = users[0]
    others = [u for u in users[1:] if u[0] != hub[0]]
    followed: list[int] = []
    for target in others[: min(3, len(others))]:
        try:
            await client.follow(hub[0], target[0])
            followed.append(target[0])
        except RuntimeError as e:
            # 业务拒绝（如对方已拉黑 → 400「无法关注」，黑名单持久化会跨 seed 命中）
            # 软降级跳过，不中断整体流程（与服务端行为一致：拉黑即互斥）
            logger.warning(f"关注失败（已跳过，可能被拉黑）: {e}")
    # 拉黑其中一个（演示黑名单场景）。**只拉黑这 1 个**——黑名单持久化且跨 seed
    # 累积，多拉一条就多一个用户被静默排除在评论 / 私信 / 关注之外（服务端静默
    # 拒绝，seed 只能软降级跳过）。历史累积的多余黑名单由后续
    # ``_normalize_blocklist`` 统一收敛到每人 1 条。
    if len(others) > 1:
        try:
            await client.block(hub[0], others[1][0])
        except RuntimeError as e:
            logger.warning(f"拉黑失败（已跳过）: {e}")
    # 3.1) 关注流验证（/feed/following 应返回关注作者的动态）
    if not skip_follow and followed:
        try:
            feed = await client.feed_following(hub[0])
            items = feed.get("items", [])
            feed_mids = {it["mid"] for it in items}
            logger.info(
                f"关注流验证：{hub[0]} 关注 {followed}，"
                f"/feed/following 返回 {len(items)} 条（作者 {sorted(feed_mids)[:10]}）"
            )
        except RuntimeError as e:
            logger.warning(f"关注流验证失败: {e}")

    # 4) 事件通知（like / reply / at 三类）
    target_mid = others[0][0] if others else hub[0]
    actor = hub
    for event_type in (InteractionActionTypeEnum.LIKE, InteractionActionTypeEnum.REPLY, InteractionActionTypeEnum.AT):
        await client.report_event(
            target_mid,
            event_type,
            _rr.pick(normal_ids),
            actor[0],
            actor[1],
            str(_rr.pick(normal_ids)),
        )

    # 5) 系统通知（root 发布，全体用户）
    await client.notify_admin_create(
        "SEED 系统通知", "这是一条由 seed 脚本发布的系统通知"
    )

    logger.success(
        f"[用户级互动] 收藏夹 {len(folder_ids)} 个，收藏 {fav_count} 次，关注/拉黑/事件/通知已覆盖"
    )
