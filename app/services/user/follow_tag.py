"""关注分组服务（对标 B 站 `x/relation/tags`）。

职责分工：本服务只管「分组定义 + 分组-成员关系」，**不重复实现关注关系读写**
（关注 / 取关仍在 `FollowService`）；列表按分组过滤也复用 `FollowService.list_following`。

系统分组（不落定义表，服务层合成）：

| tagid | 名称 | 语义 |
| --- | --- | --- |
| `-10` | 特别关注 | 关系行照常落 `msg_user_follow_tag_user`（`tag_id = -10`），但分组**定义**内置，不可删改 |
| `0` | 默认分组 | **未落到任何分组**的当前关注对象，count = 关注总数 − 各分组 count 之和 |

约束：

- 自建分组上限 `MAX_USER_TAGS = 20`（对齐 B 站「最多创建 20 个分组」）；
- 分组名 1~20 字，同用户不重名（`uq(mid, name)` 兜底，服务层先做友好校验）；
- 只允许把**我当前正在关注**的人放进分组：未关注 / 已取关的对象不写关系行
  （否则分组里会残留幽灵成员，B 站同样只对已关注用户分组）；
- 取关 / 拉黑时由 `FollowService` 调 `clear_target` 清掉该对象的所有分组关系（同事务）。
"""

from sqlalchemy import delete, func
from sqlmodel import col, select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.db.follow_tag_tbl import UserFollowTag, UserFollowTagUser
from app.models.db.follow_tbl import UserFollow
from app.models.enums import FollowStatusEnum
from app.models.schemas.follow import FollowTagResp
from app.services.user.space.membership import TagCounts, TagMembership


class FollowTagService:
    """关注分组读写。"""

    #: 特别关注（系统分组，关系可落库、定义不可增删改）
    SPECIAL_TAG_ID = -10
    #: 默认分组（不落库，永远排在特别关注之后）
    DEFAULT_TAG_ID = 0
    #: 用户自建分组上限
    MAX_USER_TAGS = 20
    #: 分组名长度上限
    MAX_TAG_NAME_LEN = 20

    # ==================== 读 ====================

    @staticmethod
    async def list_tags(session: AsyncSession, mid: int) -> list[FollowTagResp]:
        """我的分组列表（顺序：特别关注 → 默认分组 → 自建分组，与 B 站一致）。

        查询方式为**业内最常用的「多条窄查询 + 应用层拼装」**（同一个 session 顺序跑
        3 条简单查询，不写 UNION / 巨型 JOIN 缝合）：

        1. `_tag_counts`：`GROUP BY tag_id` 一次拿到各分组（含特别关注 `-10`）的成员数；
        2. `_count_default`：默认分组 = 仍在关注、且没有任何分组关系行的对象数；
        3. `_list_tag_defs`：分组定义（含 0 成员的空分组，保证不漏分组）。

        三条互相独立，各自都能稳定命中索引（`idx_follow_tag_user_tag`、
        `idx_follow_target_status`、`idx_follow_tag_mid`），执行计划简单可测；
        任何一条都能单独复用 / 单独加索引。默认分组用 `NOT EXISTS` 而不是
        「总数 − 分组和」（同一人属于多个分组时减法会算错）。

        注：`AsyncSession` 不是并发安全的，**不能**用 `asyncio.gather` 并发跑这三条；
        同 session 顺序执行复用同一条连接，省掉连接建立开销即可。
        """
        counts, default_count, defs = (
            await FollowTagService._tag_counts(session, mid),
            await FollowTagService._count_default(session, mid),
            await FollowTagService._list_tag_defs(session, mid),
        )

        tags = [
            FollowTagResp(
                tagid=FollowTagService.SPECIAL_TAG_ID,
                name="特别关注",
                count=counts.count_of(FollowTagService.SPECIAL_TAG_ID),
                tip="第一时间收到该分组下用户更新稿件的通知",
            ),
            FollowTagResp(
                tagid=FollowTagService.DEFAULT_TAG_ID,
                name="默认分组",
                count=default_count,
            ),
        ]
        tags.extend(
            FollowTagResp(
                tagid=int(t.id or 0),
                name=t.name,
                count=counts.count_of(int(t.id or 0)),
                tip=t.tip or "",
            )
            for t in defs
        )
        return tags

    @staticmethod
    async def membership_of(
        session: AsyncSession, mid: int, target_mid: int
    ) -> TagMembership:
        """某个关注对象在我这里的分组归属（分组弹窗预填用，单次等值查询）。

        返回 `TagMembership` 值对象（升序去重）而不是裸 `dict`：调用方直接用
        `membership.tag_ids` / `membership.includes(tag_id)`，无需猜「缺键」语义。
        """
        rows = (
            await session.exec(
                select(UserFollowTagUser.tag_id).where(
                    col(UserFollowTagUser.mid) == mid,
                    col(UserFollowTagUser.target_mid) == target_mid,
                )
            )
        ).all()
        return TagMembership.of(target_mid, [int(r) for r in rows])

    # ==================== 写 ====================

    @staticmethod
    async def create_tag(
        session: AsyncSession, mid: int, name: str, tip: str | None = None
    ) -> FollowTagResp:
        """新建自建分组。

        Raises:
            ValueError: 名称为空 / 过长 / 重名 / 超出分组数量上限。
        """
        tag_name = (name or "").strip()
        if not tag_name:
            raise ValueError("分组名不能为空")
        if len(tag_name) > FollowTagService.MAX_TAG_NAME_LEN:
            raise ValueError(f"分组名不能超过 {FollowTagService.MAX_TAG_NAME_LEN} 个字")

        duplicated = (
            await session.exec(
                select(func.count())
                .select_from(UserFollowTag)
                .where(
                    col(UserFollowTag.mid) == mid, col(UserFollowTag.name) == tag_name
                )
            )
        ).one()
        if int(duplicated or 0) > 0:
            raise ValueError("分组名已存在")

        owned = (
            await session.exec(
                select(func.count())
                .select_from(UserFollowTag)
                .where(col(UserFollowTag.mid) == mid)
            )
        ).one()
        if int(owned or 0) >= FollowTagService.MAX_USER_TAGS:
            raise ValueError(f"最多创建 {FollowTagService.MAX_USER_TAGS} 个分组")

        tag = UserFollowTag(mid=mid, name=tag_name, tip=(tip or "").strip() or None)
        session.add(tag)
        await session.commit()
        await session.refresh(tag)
        return FollowTagResp(
            tagid=int(tag.id or 0), name=tag.name, count=0, tip=tag.tip or ""
        )

    @staticmethod
    async def delete_tag(session: AsyncSession, mid: int, tag_id: int) -> None:
        """删除自建分组（连带清掉成员关系）。

        Raises:
            ValueError: 系统分组不可删 / 分组不存在。
        """
        if tag_id <= FollowTagService.DEFAULT_TAG_ID:
            raise ValueError("系统分组不可删除")
        if await FollowTagService._get_own_tag(session, mid, tag_id) is None:
            raise ValueError("分组不存在")
        await session.exec(
            delete(UserFollowTagUser).where(
                col(UserFollowTagUser.mid) == mid,
                col(UserFollowTagUser.tag_id) == tag_id,
            )
        )
        await session.exec(
            delete(UserFollowTag).where(
                col(UserFollowTag.id) == tag_id, col(UserFollowTag.mid) == mid
            )
        )
        await session.commit()

    @staticmethod
    async def assign(
        session: AsyncSession, mid: int, target_mid: int, tag_ids: list[int]
    ) -> list[int]:
        """把 target_mid 的分组**覆盖式**设置为 `tag_ids`。

        - 只能操作「我当前正在关注」的人（未关注直接报错，避免产生幽灵成员）；
        - 自建分组必须是我自己的分组，否则报错；
        - `tag_ids` 里的 `0`（默认分组）是合成值，直接剔除；传空数组 = 移出全部分组。

        Returns:
            设置完成后的分组 id 列表（升序去重）。
        """
        if mid == target_mid:
            raise ValueError("不能对自己分组")

        followed = (
            await session.exec(
                select(func.count())
                .select_from(UserFollow)
                .where(
                    col(UserFollow.mid) == mid,
                    col(UserFollow.target_mid) == target_mid,
                    col(UserFollow.status) == FollowStatusEnum.FOLLOWING,
                )
            )
        ).one()
        if int(followed or 0) == 0:
            raise ValueError("只能对已关注的用户分组")

        # 剔除「默认分组」合成值；-10 特别关注放行；其它负数非法
        wanted = sorted(
            {int(t) for t in tag_ids if int(t) != FollowTagService.DEFAULT_TAG_ID}
        )
        if any(t < 0 and t != FollowTagService.SPECIAL_TAG_ID for t in wanted):
            raise ValueError("非法的系统分组")

        own_ids = set(await FollowTagService._own_tag_ids(session, mid))
        if any(t > 0 and t not in own_ids for t in wanted):
            raise ValueError("分组不存在")

        # 覆盖式：先清空该对象在我全部分组里的关系，再按新集合写入
        await FollowTagService.clear_target(session, mid, target_mid)
        for tag_id in wanted:
            session.add(
                UserFollowTagUser(mid=mid, tag_id=tag_id, target_mid=target_mid)
            )
        await session.commit()
        return wanted

    @staticmethod
    async def clear_target(session: AsyncSession, mid: int, target_mid: int) -> None:
        """清掉 target_mid 在我所有分组里的关系（取关 / 拉黑时调用）。

        **不 commit**：与关注关系变更放在同一事务里提交，保证「取关」与「移出分组」原子。
        """
        await session.exec(
            delete(UserFollowTagUser).where(
                col(UserFollowTagUser.mid) == mid,
                col(UserFollowTagUser.target_mid) == target_mid,
            )
        )

    # ==================== 内部工具 ====================

    @staticmethod
    async def _get_own_tag(
        session: AsyncSession, mid: int, tag_id: int
    ) -> UserFollowTag | None:
        """取「我的」分组（带 mid 校验，避免越权删别人的分组）。"""
        rows = (
            await session.exec(
                select(UserFollowTag).where(
                    col(UserFollowTag.id) == tag_id, col(UserFollowTag.mid) == mid
                )
            )
        ).all()
        return rows[0] if rows else None

    @staticmethod
    async def _own_tag_ids(session: AsyncSession, mid: int) -> list[int]:
        """我的自建分组 id 列表（不含系统分组）。"""
        rows = (
            await session.exec(
                select(UserFollowTag.id).where(col(UserFollowTag.mid) == mid)
            )
        ).all()
        return [int(r) for r in rows]

    @staticmethod
    async def _tag_counts(session: AsyncSession, mid: int) -> TagCounts:
        """各分组当前成员数（`TagCounts` 值对象，一次 `GROUP BY`，含特别关注 `-10`）。

        走 `idx_follow_tag_user_tag`；与关注表内连接，只统计仍在关注的对象，
        取关残留的脏关系行不会抬高计数。
        """
        rows = (
            await session.exec(
                select(UserFollowTagUser.tag_id, func.count())
                .join(
                    UserFollow,
                    (col(UserFollow.mid) == mid)
                    & (col(UserFollow.target_mid) == col(UserFollowTagUser.target_mid))
                    & (col(UserFollow.status) == FollowStatusEnum.FOLLOWING),
                )
                .where(col(UserFollowTagUser.mid) == mid)
                .group_by(col(UserFollowTagUser.tag_id))
            )
        ).all()
        return TagCounts.of([(int(tag_id), int(cnt or 0)) for tag_id, cnt in rows])

    @staticmethod
    async def _count_default(session: AsyncSession, mid: int) -> int:
        """默认分组成员数 = 仍在关注、且没有任何分组关系行的对象数。

        用 `NOT EXISTS` 而不是「关注总数 − 各分组之和」：一个人可以同时属于多个分组，
        减法会把重复计数算进来导致默认分组偏小。
        """
        tagged = select(UserFollowTagUser.id).where(
            col(UserFollowTagUser.mid) == mid,
            col(UserFollowTagUser.target_mid) == col(UserFollow.target_mid),
        )
        total = (
            await session.exec(
                select(func.count())
                .select_from(UserFollow)
                .where(
                    col(UserFollow.mid) == mid,
                    col(UserFollow.status) == FollowStatusEnum.FOLLOWING,
                    ~tagged.exists(),
                )
            )
        ).one()
        return int(total or 0)

    @staticmethod
    async def _list_tag_defs(session: AsyncSession, mid: int) -> list[UserFollowTag]:
        """我的分组定义（按创建时间升序，与 B 站「先建的在前」一致）。

        计数查询只覆盖「有成员的分组」，所以定义必须单独查，空分组才不会丢。
        走 `idx_follow_tag_mid`。
        """
        rows = (
            await session.exec(
                select(UserFollowTag)
                .where(col(UserFollowTag.mid) == mid)
                .order_by(col(UserFollowTag.created_at).asc())
            )
        ).all()
        return list(rows)


__all__ = ["FollowTagService"]
