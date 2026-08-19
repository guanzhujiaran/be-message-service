"""评论领域删除（`cleanup_comment`）。

清除指定 uid 用户在 be-message MySQL 的评论相关全部数据：

- `msg_comment_index`(mid)：该用户发表的评论（软删置 DELEted 即可？否——注销需物理删除，
  但正文 `msg_comment_content` 无独立 mid，需先按 `index.rpid` 联动删正文）；
- `msg_comment_content`(rpid IN index WHERE mid=uid)：评论正文，先删（依赖 index 的 rpid）；
- `msg_comment_action`(mid)：点赞等互动痕迹；
- `msg_comment_at`(from_mid/at_mid)：@ 关系双向；
- `msg_comment_subject`(up_mid)：该用户作为内容作者的评论主体。

顺序关键：先删 content（按 index rpid），再删 index；at/action/subject 独立。
"""

from sqlalchemy import or_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col, delete, select

from app.models.db import (
    CommentAction,
    CommentAt,
    CommentContent,
    CommentIndex,
    CommentSubject,
)


class CleanupCommentService:
    """评论领域删除（静态方法集合）。"""

    @staticmethod
    async def delete_all_by_uid(session: AsyncSession, uid: int) -> None:
        """删除指定 uid 用户的全部评论数据（调用方负责 commit）。"""
        # 评论正文（无独立 mid，先按 index 的 rpid 联动删）
        await session.exec(
            delete(CommentContent).where(
                col(CommentContent.rpid).in_(
                    select(CommentIndex.rpid).where(col(CommentIndex.mid) == uid)
                )
            )
        )
        # 该用户发表的评论索引行
        await session.exec(delete(CommentIndex).where(col(CommentIndex.mid) == uid))
        # 评论互动痕迹（点赞/踩等）
        await session.exec(delete(CommentAction).where(col(CommentAction.mid) == uid))
        # @ 关系（双向）
        await session.exec(
            delete(CommentAt).where(
                or_(col(CommentAt.from_mid) == uid, col(CommentAt.at_mid) == uid)
            )
        )
        # 该用户作为内容作者（up_mid）的评论主体
        await session.exec(
            delete(CommentSubject).where(col(CommentSubject.up_mid) == uid)
        )


__all__ = ["CleanupCommentService"]
