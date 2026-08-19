"""杂项领域删除（`cleanup_misc`）。

清除指定 uid 用户的「设置 / 活跃 / 封禁 / 管理」相关数据：

- `msg_user_setting`(mid)：消息设置；
- `msg_user_activity`(mid)：活跃度；
- `msg_user_ban`(mid/operator_mid)：封禁关系（被禁者 / 操作者双向）；
- `msg_admin`(mid/granted_by)：管理端授权（被授权人 / 授予人双向）。
"""

from sqlalchemy import or_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col, delete

from app.models.db import MessageAdmin, UserActivity, UserBan, UserMessageSetting


class CleanupMiscService:
    """杂项领域删除（静态方法集合）。"""

    @staticmethod
    async def delete_all_by_uid(session: AsyncSession, uid: int) -> None:
        """删除指定 uid 用户的设置 / 活跃 / 封禁 / 管理数据（调用方负责 commit）。"""
        await session.exec(
            delete(UserMessageSetting).where(col(UserMessageSetting.mid) == uid)
        )
        await session.exec(delete(UserActivity).where(col(UserActivity.mid) == uid))
        await session.exec(
            delete(UserBan).where(
                or_(col(UserBan.mid) == uid, col(UserBan.operator_mid) == uid)
            )
        )
        await session.exec(
            delete(MessageAdmin).where(
                or_(col(MessageAdmin.mid) == uid, col(MessageAdmin.granted_by) == uid)
            )
        )


__all__ = ["CleanupMiscService"]
