"""私信领域删除（`cleanup_dm`）。

清除指定 uid 用户的私信相关全部数据（写扩散，双向关系需按双方字段删）：

- `msg_dm_session`(owner_mid/talker_mid)：会话；
- `msg_dm_index`(owner_mid/sender_uid)：消息索引（owner 视角 + sender 视角）；
- `msg_dm_content_dlq`(sender_uid/receiver_uid)：私信内容死信表。

注意：私信**内容分片**（`bili_msg_content_YYYYMM` 月度库）不在主库，由
`DmContentService` 动态路由，注销时仅清理主库索引/会话/死信记录。
"""

from sqlalchemy import or_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col, delete

from app.models.db import DmContentDeadLetter, DmMessageIndex, DmSession


class CleanupDmService:
    """私信领域删除（静态方法集合）。"""

    @staticmethod
    async def delete_all_by_uid(session: AsyncSession, uid: int) -> None:
        """删除指定 uid 用户的全部私信数据（调用方负责 commit）。"""
        await session.exec(
            delete(DmSession).where(
                or_(col(DmSession.owner_mid) == uid, col(DmSession.talker_mid) == uid)
            )
        )
        await session.exec(
            delete(DmMessageIndex).where(
                or_(col(DmMessageIndex.owner_mid) == uid, col(DmMessageIndex.sender_uid) == uid)
            )
        )
        await session.exec(
            delete(DmContentDeadLetter).where(
                or_(
                    col(DmContentDeadLetter.sender_uid) == uid,
                    col(DmContentDeadLetter.receiver_uid) == uid,
                )
            )
        )


__all__ = ["CleanupDmService"]
