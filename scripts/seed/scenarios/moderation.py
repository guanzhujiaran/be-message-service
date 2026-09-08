"""场景④-extra 管理侧动作：通用资源计数 + 用户空间举报 + 封禁 + 头像审核流。

与私信场景同一批用户；封禁落在最后一个用户上，演示后不影响主链路。
"""
from loguru import logger

from ..client import SeedClient
from ..material import _IMG_URLS
from ..rr import _rr


async def seed_moderation(client: SeedClient, users: list[tuple[int, str | None]]) -> None:
    """管理侧动作：通用计数 → 用户举报 → 封禁 → 头像审核（提交 + 管理员通过）。"""
    # 2) 通用互动计数：lottery 资源点赞（TInteractionStat）
    for mid, _ in users[: min(2, len(users))]:
        await client.thumb_lottery(mid, 10000000 + _rr.pick(range(90000000)))

    # 3) 用户空间举报
    await client.report_user(users[1][0], users[0][0])

    # 4) 封禁（root，comment 服务，临时 7 天）—— 封禁最后一个用户，演示后不影响主链路
    ban_target = users[-1][0]
    if ban_target != users[0][0]:
        try:
            await client.ban_user(ban_target)
        except RuntimeError as e:
            logger.warning(f"封禁失败: {e}")

    # 5) 头像审核流：提交头像（带 .jpg 后缀真实图源）→ 管理员审核通过
    avatar_mid = users[0][0]
    await client.submit_avatar(avatar_mid, _rr.pick(_IMG_URLS))
    await client.approve_avatar()
