"""大数据灌数：真实话题的幂等创建 + 审核通过（复用库里已有话题）。"""
import asyncio

from loguru import logger
from tqdm import tqdm

from ..client import SeedClient
from ..datasource import _load_existing_topics


async def _seed_topics(
    client: SeedClient,
    names: list[str],
    admin_mid: int,
    concurrency: int,
    existing: dict[str, int],
) -> list[int]:
    """并发创建并审核真实话题，复用已存在话题（幂等），返回 topicId 列表。

    ``existing`` 为 name->topicId 映射（预拉 + 创建时增量更新），用于幂等复用，
    避免重复话题返回 422 后被丢弃导致动态无法挂话题。
    """
    sem = asyncio.Semaphore(concurrency)

    async def one(name: str) -> int | None:
        name = name.strip()
        if not name:
            return None
        async with sem:
            # 1) 已存在则直接复用，否则创建
            if name in existing:
                tid = existing[name]
            else:
                try:
                    tid = await client.create_topic(admin_mid, name)
                    existing[name] = int(tid)
                    tid = int(tid)
                except Exception as e:  # noqa: BLE001
                    # 已存在（或响应丢失但后端已建）：回查全量复用
                    existing.update(await _load_existing_topics())
                    if name in existing:
                        tid = existing[name]
                    else:
                        logger.warning(f"话题「{name}」创建失败: {e}")
                        return None
            # 2) 确保审核通过：复用的 auditing 话题 / 新建话题都需 normal，否则动态关联会 400
            try:
                await client.approve_topic(tid)
            except Exception:  # noqa: BLE001
                pass  # 已是 normal（或状态异常），忽略
            return tid

    tasks = [asyncio.create_task(one(n)) for n in names]
    results: list[int | None] = []
    for f in tqdm(
        asyncio.as_completed(tasks),
        total=len(tasks),
        desc="seed topics",
        unit="个",
    ):
        try:
            results.append(await f)
        except Exception:  # noqa: BLE001
            pass
    return [t for t in results if isinstance(t, int)]
