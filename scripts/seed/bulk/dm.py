"""大数据灌数：pptr 用户池 O(n²) 批量互发私信，填充 DM 内容（软降级跳过业务拒绝）。"""
import asyncio

from loguru import logger
from tqdm import tqdm

from ..blocklist import _is_dm_blocked
from ..client import SeedClient


async def _seed_bulk_dm(
    client: SeedClient,
    users: list[tuple[int, str | None]],
    count: int,
    dm_concurrency: int,
    blocked: set[tuple[int, int]] | None = None,
) -> None:
    """大数据灌数：在 pptr 用户池上批量互发私信，填充 DM 内容（软降级跳过业务拒绝）。

    复用与 ``seed_message`` 场景 E 一致的 O(n²) 遍历逻辑：取前 ``count`` 个有序用户对
    （剔除「接收方已拉黑发送方」方向），每对发 1 条并审核通过。覆盖「用户对网络」广度，
    使 pptr 用户池内任意用户（如登录查看 demo 的测试账号）都有私信内容可读。
    接收方关闭陌生人私信时，来自陌生人的消息被过滤（仅发送方自见），此处宽容跳过该对，
    不阻断整体；发送方自己的消息始终可见，故发起方视角必有私信内容。
    """
    if len(users) < 2:
        logger.warning("用户数不足，跳过大数据灌数私信。")
        return
    blocked = blocked or set()
    sem = asyncio.Semaphore(dm_concurrency)

    async def _send_one_directed(sender, receiver) -> bool:
        """单向发 1 条并审核通过；业务拒绝返回 False（不报 warning）。"""
        async with sem:
            try:
                mk = await client.dm_send(sender[0], receiver[0], receiver[1])
                await client.approve_dm(mk)
                return True
            except Exception as e:  # noqa: BLE001
                msg = str(e)
                if any(k in msg for k in ("拉黑", "黑名单", "陌生", "对方已")):
                    logger.info(
                        f"批量私信 {sender[0]}→{receiver[0]} 被业务规则拒绝（预期内跳过）: {msg}"
                    )
                else:
                    logger.warning(
                        f"批量私信 {sender[0]}→{receiver[0]} 发送失败（跳过）: {e}"
                    )
                return False

    # O(n²) 遍历全部有序用户对（剔除「接收方已拉黑发送方」的必拒方向），
    # 取前 count 对——每对只发 1 条，故总发送量 = min(count, 可发对数)
    directed = [
        (sender, receiver)
        for sender in users
        for receiver in users
        if sender[0] != receiver[0]
        and not _is_dm_blocked(blocked, sender[0], receiver[0])
    ][:count]

    total = 0
    if directed:
        pending = [
            asyncio.create_task(_send_one_directed(s, r)) for s, r in directed
        ]
        for f in tqdm(
            asyncio.as_completed(pending),
            total=len(pending),
            desc="bulk dm",
            unit="条",
        ):
            try:
                if await f:
                    total += 1
            except Exception:  # noqa: BLE001
                pass
    logger.success(
        f"[大数据灌数] 私信填充完成：遍历用户对取前 {len(directed)} 对（每对 1 条），"
        f"实际发送 {total} 条（目标 {count} 条）"
    )
