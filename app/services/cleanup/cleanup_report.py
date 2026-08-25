"""举报领域删除（`cleanup_report`）。

清除指定 uid 用户在 be-message MySQL 的举报相关全部数据。三类举报表均继承
bili-common `ReportBase`（含 `reportMid` / `accusedMid`），按「举报人 / 被举报人」
双向删除：

- `TResourceReport`(bizType=dynamic)
- `CommentReport`（`msg_comment_report`，bizType=comment）
- `TUserReport`(bizType=user)
"""

from sqlalchemy import or_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col, delete

from app.models.db import CommentReport, TResourceReport, TUserReport


class CleanupReportService:
    """举报领域删除（静态方法集合）。"""

    @staticmethod
    async def delete_all_by_uid(session: AsyncSession, uid: int) -> None:
        """删除指定 uid 用户的全部举报数据（调用方负责 commit）。"""
        for model in (TResourceReport, CommentReport, TUserReport):
            await session.exec(
                delete(model).where(
                    or_(col(model.reportMid) == uid, col(model.accusedMid) == uid)
                )
            )


__all__ = ["CleanupReportService"]
