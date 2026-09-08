"""lottery 资源：真实 lottery_id 获取 + 动态/lottery 混合资源池。

lottery 属**通用资源**：点赞走 ``do_like_generic``，后端**不自动生成 LIKE 事件**
（动态点赞会生成），必须由 seed 显式补发，否则消息中心「收到的赞」永远看不到抽奖资源的赞。
"""
from itertools import zip_longest

import aiomysql
import httpx
from bili_common.models import InteractionActionTypeEnum, InteractionBizTypeEnum
from loguru import logger

from .client import SeedClient
from .config import _DYN_POOL_SIZE, _LOTTERY_POOL_SIZE
from .datasource import _raw_conn


async def _fetch_lottery_ids(n: int = 1) -> list[int]:
    """从 dyndetail.lotdata 直连取真实 lottery_id（与 mysql_message_url 同 MySQL 实例）。

    lottery 资源由 crawler RPC 校验存在性，其底层即读此表；seed 直连取一个真实
    lottery_id 作为「资源」做点赞/评论/@ 联调。失败（库/表不存在、无数据）降级返回空列表。
    """
    try:
        conn = await aiomysql.connect(**_raw_conn("dyndetail"))
        try:
            cur = await conn.cursor()
            await cur.execute(
                "SELECT lottery_id FROM lotdata ORDER BY lottery_time DESC LIMIT %s", (n,)
            )
            rows = await cur.fetchall()
        finally:
            conn.close()
        return [int(r[0]) for r in rows]
    except Exception as e:  # noqa: BLE001
        logger.warning(f"直连 dyndetail.lotdata 取 lottery_id 失败（降级空）: {e}")
        return []


async def _fetch_lottery_ids_via_api(
    crawler_base_url: str, n: int = 1
) -> list[int]:
    """调 crawler 的 GetAllLottery HTTP 接口取真实 lottery_id（走接口，不直连库）。

    路径：``{crawler_base_url}/api/v1/lottery_database/bili/GetAllLottery`` (POST)。
    优先使用此方式；失败时由调用方降级到 ``_fetch_lottery_ids``（直连 dyndetail.lotdata）。
    """
    try:
        url = (
            f"{crawler_base_url.rstrip('/')}"
            "/api/v1/lottery_database/bili/GetAllLottery"
        )
        async with httpx.AsyncClient(timeout=10.0) as cli:
            resp = await cli.post(
                url, params={"page_num": 1, "page_size": max(1, n)}
            )
            resp.raise_for_status()
            payload = resp.json()
        data = (payload or {}).get("data") or {}
        ids: list[int] = []
        for key in ("common_lottery", "reserve_lottery", "official_lottery"):
            for item in data.get(key) or []:
                lid = item.get("lottery_id") if isinstance(item, dict) else None
                if lid is not None:
                    ids.append(int(lid))
        return ids[:n]
    except Exception as e:  # noqa: BLE001
        logger.warning(
            f"调 crawler GetAllLottery 接口取 lottery_id 失败（降级直连库）: {e}"
        )
        return []


def _build_resource_pool(
    normal_ids: list[int],
    lottery_ids: list[int],
    *,
    dyn_cap: int = _DYN_POOL_SIZE,
    lottery_cap: int = _LOTTERY_POOL_SIZE,
) -> list[tuple[int, InteractionBizTypeEnum]]:
    """把动态与 lottery 资源**交错**成统一互动池 ``[(oid, biz_type)]``。

    交错（``zip_longest``）而非拼接：保证两种 biz_type 在列表前后都有覆盖——
    拼接会让「前半段全是动态、后半段全是抽奖」，一旦中途失败或叠加 ``--skip-*``，
    某一种资源就完全没有数据。
    """
    dyn = [(int(i), InteractionBizTypeEnum.DYNAMIC) for i in normal_ids[:dyn_cap]]
    lot = [(int(i), InteractionBizTypeEnum.LOTTERY) for i in lottery_ids[:lottery_cap]]
    pool: list[tuple[int, InteractionBizTypeEnum]] = []
    for pair in zip_longest(dyn, lot):
        pool.extend(p for p in pair if p is not None)
    return pool


async def seed_lottery_resource(
    client: SeedClient,
    users: list[tuple[int, str | None]],
    crawler_base_url: str = "http://be-bilibili-crawler:23333",
) -> list[int]:
    """③-extra 取真实 lottery_id 并补上**通用资源独有的 LIKE 事件**缺口。

    lottery 的评论 / @ 已由 ``seed_comment`` 的混合资源池统一覆盖，此处只补齐
    动态点赞不会遇到的问题：通用资源点赞走 ``do_like_generic``，**后端不自动生成
    LIKE 事件**（动态点赞会生成），必须显式上报，否则消息中心「收到的赞」永远
    看不到抽奖资源的赞。事件统一落点同一测试用户，便于登录消息中心查看。

    Returns:
        真实 lottery_id 列表（供调用方并入混合资源池），取不到时为空列表。
    """
    if not users:
        return []
    lottery_ids = await _fetch_lottery_ids_via_api(crawler_base_url, _LOTTERY_POOL_SIZE)
    if not lottery_ids:
        lottery_ids = await _fetch_lottery_ids(
            _LOTTERY_POOL_SIZE
        )  # 接口不可达时直连库兜底
    if not lottery_ids:
        logger.warning("无可用 lottery_id（接口与直连库均失败），跳过 lottery 资源互动")
        return []
    lottery_id = lottery_ids[0]
    recipient = users[0][0]  # LIKE 事件统一落点（可登录查看消息中心）
    others = [u for u in users if u[0] != recipient] or users

    # 点赞（likeCount++；后端不自动生成事件 → 显式补 LIKE 事件给 recipient）
    for u in others[:3]:
        await client.thumb_lottery(u[0], lottery_id)
    if others:
        await client.report_event(
            recipient,
            InteractionActionTypeEnum.LIKE,
            lottery_id,
            others[0][0],
            others[0][1],
            str(lottery_id),
            biz_type=InteractionBizTypeEnum.LOTTERY,
        )

    logger.success(
        f"[lottery 资源互动] lottery_id={lottery_id}：点赞×{min(3, len(others))} + "
        f"LIKE 事件→{recipient} 已补发（评论/@ 由混合资源池统一覆盖）；"
        f"可登录 {recipient} 查看消息中心「收到的赞」并验证自动已读"
    )
    return lottery_ids
