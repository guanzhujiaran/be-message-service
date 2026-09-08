"""阶段一编排：全互动联调（``seed()``）。

顺序：动态 → lottery 资源 → 评论（混合资源池）→ 用户级互动 → 黑名单归一 → 消息与管理。
动态是后续场景的上游产物：若 0 条成功则响亮报错并终止，避免在空资源池上静默空跑。
"""
from loguru import logger

from .blocklist import _normalize_blocklist
from .client import SeedClient
from .config import _SEED_REQ_CONCURRENCY
from .datasource import _fetch_real_users
from .lottery import _build_resource_pool, seed_lottery_resource
from .material import _load_material_pools
from .scenarios import seed_comment, seed_interact, seed_message, seed_moment


async def seed(
    *,
    base_url: str,
    admin_mid: int,
    count: int,
    users_n: int,
    moment_concurrency: int,
    skip_moment: bool,
    skip_comment: bool,
    skip_interact: bool,
    skip_message: bool,
    skip_follow: bool,
    crawler_base_url: str = "http://be-bilibili-crawler:23333",
) -> None:
    # 素材池真实化：从 biliopusdb / bilidb 拉取真实素材（失败降级内置兜底）
    await _load_material_pools()
    real_users = await _fetch_real_users(users_n)
    if not real_users:
        logger.error("没有可用作作者的真实用户，终止。")
        return
    users = real_users[:users_n]

    async with SeedClient(base_url, admin_mid, req_concurrency=_SEED_REQ_CONCURRENCY) as client:
        normal_ids: list[int] = []

        if not skip_moment:
            normal_ids = await seed_moment(
                client, users, count, concurrency=moment_concurrency
            )
            if not normal_ids:
                # 动态是评论/互动/私信的上游产物：若 count 条一条都没打通，
                # 后续只能在空资源池上静默空跑（失败全被软降级成 warning，ERROR 过滤后无任何输出）。
                # 必须在「报错判断」处响亮打断，否则控制台将一片空白、无从定位。
                logger.error(
                    f"阶段一：动态创建 0 条成功（共尝试 {count} 条，全部失败/被软降级跳过），"
                    "动态链路未打通，终止后续评论/互动/私信以避免在空资源上静默空跑。"
                )
                return
        # lottery 资源：取真实 lottery_id 并补 LIKE 事件（通用资源点赞后端不自动
        # 生成事件，动态点赞会生成）。返回的 id 并入下面的混合资源池，
        # 让评论 / at / 点赞一并覆盖到通用资源链路。
        lottery_ids: list[int] = []
        if not (skip_comment and skip_interact):
            lottery_ids = await seed_lottery_resource(
                client, users, crawler_base_url=crawler_base_url
            )

        if not skip_comment:
            await seed_comment(
                client, users, _build_resource_pool(normal_ids, lottery_ids)
            )
        if not skip_interact:
            await seed_interact(
                client,
                users,
                normal_ids or await _fallback_normal_ids(client),
                skip_follow=skip_follow,
            )
        # 黑名单归一：每人只保留 1 条（够验证「拉黑后不可互动」），
        # 历史累积的黑名单会持续吃掉评论 / 私信 / 关注 / @ 通知的覆盖率。
        # 归一结果透传给私信场景做配对避让。
        blocked = await _normalize_blocklist(client, users)
        if not skip_message:
            await seed_message(client, users, count, moment_concurrency, blocked)

        logger.success("全互动 seed 执行完成。")


async def _fallback_normal_ids(client: SeedClient) -> list[int]:
    """跳过动态模块时，从综合 Feed 拉取已过审动态作互动目标。"""
    try:
        data = await client._get(
            "/api/v1/community/feed/all", client.admin_mid, {"ps": 20, "sort": "time"}
        )
        return [int(it["dynId"]) for it in data.get("items", [])]
    except RuntimeError:
        return []
