"""be-message 只读探针：动作前查库确认前置数据存在（模拟真实用户操作）。

只读取；写操作一律走 HTTP 业务接口。
"""
from loguru import logger
from sqlmodel import select

from app.core.database import new_session
from app.models.db.dm_tbl import DmMessageIndex, DmSession
from app.models.db.setting_tbl import UserMessageSetting
from app.models.enums import DmRelationEnum

from .blocklist import _is_dm_blocked

# ---------------------------------------------------------------------------
# be-message MySQL 只读探针：每个动作前查询数据库确认前置数据存在，模拟真实用户操作
# ---------------------------------------------------------------------------


async def _accept_stranger_dm(receiver_mid: int) -> bool:
    """查询接收方是否接受陌生人私信（msg_user_setting；无记录默认 True）。"""
    async with new_session() as s:
        row = (
            await s.exec(
                select(UserMessageSetting).where(UserMessageSetting.mid == receiver_mid)
            )
        ).one_or_none()
    return bool(row.recv_stranger_dm) if row else True


async def _session_relation(owner_mid: int, talker_mid: int) -> DmRelationEnum | None:
    """查询 owner 视角会话关系（msg_dm_session；无会话返回 None）。"""
    async with new_session() as s:
        row = (
            await s.exec(
                select(DmSession).where(
                    DmSession.owner_mid == owner_mid,
                    DmSession.talker_mid == talker_mid,
                )
            )
        ).one_or_none()
    return row.relation if row else None


async def _dm_index_by_msgkey(msgkey: int) -> list[DmMessageIndex]:
    """查询某条私信的双方索引行（msg_dm_index），确认写扩散落库。"""
    async with new_session() as s:
        rows = (
            await s.exec(select(DmMessageIndex).where(DmMessageIndex.msgkey == msgkey))
        ).all()
    return list(rows)


async def _pick_dm_pair(
    users: list[tuple[int, str | None]],
    blocked: set[tuple[int, int]] | None = None,
) -> tuple[tuple[int, str | None], tuple[int, str | None]]:
    """选一对可正常收发私信的用户：接收方须接受陌生人私信（查 msg_user_setting）。

    同时按 ``blocked`` 双向避让——a→b 与 b→a 任一方向会被黑名单拒绝就换下一对，
    避免深度撤回 / 删除场景（断言响亮报错）因历史黑名单而整段失败。
    先取前若干对尝试，避免整个 seed 因发送被 filtered 而失败。
    """
    blocked = blocked or set()
    for i in range(min(5, len(users) - 1)):
        a, b = users[i], users[i + 1]
        if _is_dm_blocked(blocked, a[0], b[0]) or _is_dm_blocked(blocked, b[0], a[0]):
            logger.warning(f"用户对 {a[0]}<->{b[0]} 存在黑名单关系，尝试下一对…")
            continue
        if await _accept_stranger_dm(b[0]):
            return a, b
        logger.warning(f"用户 {b[0]} 关闭了陌生人私信，尝试下一对…")
    # 兜底：直接用前两个用户（即使可能被过滤，发送方视角仍会写入）
    return users[0], users[1]
