"""用户关注 / 拉黑服务。

提供关注 / 取关 / 拉黑 / 解除拉黑 / 关系查询 / 关注与粉丝列表。
关系数据自包含在 `msg_user_follow`，**不回写 pptr**，与用户主数据解耦：

- 用户展示信息（昵称 / 头像 / 等级 / 大会员等）只有一份，在 pptr 的 Postgres，
  本服务返回的列表仅含 `mid` 与关系建立时间，前端按 mid 批量回查 pptr 即可。
- 关注关系是单向的，互相关注需要两条 `following` 记录（双向各一）。

并发与幂等：
- `uq(mid, target_mid)` 兜底，重复关注 / 拉黑由数据库侧拦掉；
- 关注用 `INSERT ... ON DUPLICATE KEY UPDATE` 做幂等 upsert，状态翻转（拉黑→关注）
  也在同一语句内完成；
- 拉黑会同时删除对方对自己的 `following` 记录（即对方不再是自己的粉丝），
  保证拉黑后对方既不能关注、也不能再看到自己的关注关系。
"""

from datetime import datetime

from loguru import logger
from sqlalchemy import and_, delete, func
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.orm import aliased
from sqlmodel import col, select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.db.follow import UserFollow
from app.models.enums import FollowStatusEnum
from app.models.schemas.follow import (
    FollowCountResp,
    FollowListItem,
    FollowListResp,
    FollowOpResp,
    FollowRelationResp,
)


class FollowService:
    """关注 / 拉黑读写。"""

    # ==================== 写操作 ====================

    @staticmethod
    async def follow(session: AsyncSession, mid: int, target_mid: int) -> FollowOpResp:
        """关注 target_mid。

        幂等：已关注则保持 `following`；此前拉黑过对方则状态翻转为 `following`。
        若对方已拉黑我，则拒绝关注（不能关注一个拉黑了自己的人）。

        Raises:
            ValueError: 不能关注自己 / 对方已拉黑你。
        """
        if mid == target_mid:
            raise ValueError("不能关注自己")

        # 拉黑方向校验：target_mid → mid 是否为 blocked
        if await FollowService._relation_status(session, target_mid, mid) == FollowStatusEnum.BLOCKED:
            raise ValueError("对方已拉黑你，无法关注")

        now = datetime.now()
        stmt = (
            mysql_insert(UserFollow.__table__)  # type: ignore[attr-defined]
            .values(
                mid=mid,
                target_mid=target_mid,
                status=FollowStatusEnum.FOLLOWING.value,
                created_at=now,
                updated_at=now,
            )
            # 命中 uq(mid, target_mid) 时改状态为 following（兼带拉黑→关注的翻转）
            .on_duplicate_key_update(
                status=FollowStatusEnum.FOLLOWING.value,
                updated_at=now,
            )
        )
        await session.exec(stmt)  # type: ignore[call-overload]
        await session.commit()
        logger.debug(f"用户 {mid} 关注 {target_mid}")
        return await FollowService._build_op_resp(session, mid, target_mid)

    @staticmethod
    async def unfollow(
        session: AsyncSession, mid: int, target_mid: int
    ) -> FollowOpResp:
        """取关 target_mid。

        仅删除 `following` 记录；若存在 `blocked` 记录则保留（取关不等于解除拉黑）。
        幂等：未关注时也返回成功。
        """
        if mid == target_mid:
            raise ValueError("不能取关自己")

        await session.exec(
            delete(UserFollow).where(
                col(UserFollow.mid) == mid,
                col(UserFollow.target_mid) == target_mid,
                col(UserFollow.status) == FollowStatusEnum.FOLLOWING,
            )
        )
        await session.commit()
        return await FollowService._build_op_resp(session, mid, target_mid)

    @staticmethod
    async def block(session: AsyncSession, mid: int, target_mid: int) -> FollowOpResp:
        """拉黑 target_mid。

        - 幂等：已拉黑则保持 `blocked`；
        - 若此前关注过对方（mid → target_mid 为 `following`），状态翻转为 `blocked`；
        - **同时删除对方对自己的 `following` 记录**（target_mid → mid），使对方不再是
          自己的粉丝，且后续对方也无法重新关注自己。

        Raises:
            ValueError: 不能拉黑自己。
        """
        if mid == target_mid:
            raise ValueError("不能拉黑自己")

        now = datetime.now()
        # 1. upsert mid → target_mid 为 blocked
        stmt = (
            mysql_insert(UserFollow.__table__)  # type: ignore[attr-defined]
            .values(
                mid=mid,
                target_mid=target_mid,
                status=FollowStatusEnum.BLOCKED.value,
                created_at=now,
                updated_at=now,
            )
            .on_duplicate_key_update(
                status=FollowStatusEnum.BLOCKED.value,
                updated_at=now,
            )
        )
        await session.exec(stmt)  # type: ignore[call-overload]

        # 2. 删除 target_mid → mid 的 following（如有），让对方不再是自己的粉丝
        await session.exec(
            delete(UserFollow).where(
                col(UserFollow.mid) == target_mid,
                col(UserFollow.target_mid) == mid,
                col(UserFollow.status) == FollowStatusEnum.FOLLOWING,
            )
        )
        await session.commit()
        logger.debug(f"用户 {mid} 拉黑 {target_mid}")
        return await FollowService._build_op_resp(session, mid, target_mid)

    @staticmethod
    async def unblock(
        session: AsyncSession, mid: int, target_mid: int
    ) -> FollowOpResp:
        """解除拉黑 target_mid。

        仅删除 `blocked` 记录；若存在 `following` 记录则保留（解除拉黑不等于取关）。
        幂等：未拉黑时也返回成功。
        """
        if mid == target_mid:
            raise ValueError("不能解除拉黑自己")

        await session.exec(
            delete(UserFollow).where(
                col(UserFollow.mid) == mid,
                col(UserFollow.target_mid) == target_mid,
                col(UserFollow.status) == FollowStatusEnum.BLOCKED,
            )
        )
        await session.commit()
        return await FollowService._build_op_resp(session, mid, target_mid)

    # ==================== 关系查询 ====================

    @staticmethod
    async def get_relation(
        session: AsyncSession, mid: int, target_mid: int
    ) -> FollowRelationResp:
        """查询我与 target_mid 的双向关系。"""
        forward = await FollowService._relation_status(session, mid, target_mid)
        reverse = await FollowService._relation_status(session, target_mid, mid)
        return FollowRelationResp(
            mid=mid,
            target_mid=target_mid,
            following=forward == FollowStatusEnum.FOLLOWING,
            followed_by=reverse == FollowStatusEnum.FOLLOWING,
            mutual=(
                forward == FollowStatusEnum.FOLLOWING
                and reverse == FollowStatusEnum.FOLLOWING
            ),
            i_blocked=forward == FollowStatusEnum.BLOCKED,
            blocked_by=reverse == FollowStatusEnum.BLOCKED,
        )

    @staticmethod
    async def get_counts(session: AsyncSession, mid: int) -> FollowCountResp:
        """统计用户的关注数 / 粉丝数 / 互相关注数。"""
        following_count = int(
            (
                await session.exec(
                    select(func.count())
                    .select_from(UserFollow)
                    .where(
                        col(UserFollow.mid) == mid,
                        col(UserFollow.status) == FollowStatusEnum.FOLLOWING,
                    )
                )
            ).one()
            or 0
        )
        follower_count = int(
            (
                await session.exec(
                    select(func.count())
                    .select_from(UserFollow)
                    .where(
                        col(UserFollow.target_mid) == mid,
                        col(UserFollow.status) == FollowStatusEnum.FOLLOWING,
                    )
                )
            ).one()
            or 0
        )
        # 互相关注：mid → X 为 following 且 X → mid 也为 following
        # 用自连接一次性算出，避免拉全表回内存
        Forward = aliased(UserFollow, name="uf_forward")
        Reverse = aliased(UserFollow, name="uf_reverse")
        mutual_count = int(
            (
                await session.exec(
                    select(func.count())
                    .select_from(Forward)
                    .join(
                        Reverse,
                        and_(
                            col(Reverse.mid) == Forward.target_mid,
                            col(Reverse.target_mid) == Forward.mid,
                            col(Reverse.status) == FollowStatusEnum.FOLLOWING,
                        ),
                    )
                    .where(
                        col(Forward.mid) == mid,
                        col(Forward.status) == FollowStatusEnum.FOLLOWING,
                    )
                )
            ).one()
            or 0
        )
        return FollowCountResp(
            mid=mid,
            following_count=following_count,
            follower_count=follower_count,
            mutual_count=mutual_count,
        )

    @staticmethod
    async def list_following(
        session: AsyncSession,
        mid: int,
        page_num: int = 1,
        page_size: int = 20,
    ) -> FollowListResp:
        """我关注的人列表（按关注时间倒序）。

        `mutual` 字段会标记该用户是否与我互相关注，前端可据此展示「互相关注」徽标。
        """
        total = int(
            (
                await session.exec(
                    select(func.count())
                    .select_from(UserFollow)
                    .where(
                        col(UserFollow.mid) == mid,
                        col(UserFollow.status) == FollowStatusEnum.FOLLOWING,
                    )
                )
            ).one()
            or 0
        )
        rows = (
            await session.exec(
                select(UserFollow)
                .where(
                    col(UserFollow.mid) == mid,
                    col(UserFollow.status) == FollowStatusEnum.FOLLOWING,
                )
                .order_by(col(UserFollow.created_at).desc())
                .offset((page_num - 1) * page_size)
                .limit(page_size)
            )
        ).all()

        target_mids = [r.target_mid for r in rows]
        mutual_set = await FollowService._mutual_mids(session, mid, target_mids)

        items = [
            FollowListItem(
                mid=r.target_mid,
                created_at=r.created_at,
                mutual=r.target_mid in mutual_set,
            )
            for r in rows
        ]
        return FollowListResp(
            items=items, total=total, page_num=page_num, page_size=page_size
        )

    @staticmethod
    async def list_followers(
        session: AsyncSession,
        mid: int,
        page_num: int = 1,
        page_size: int = 20,
    ) -> FollowListResp:
        """我的粉丝列表（按关注时间倒序）。"""
        total = int(
            (
                await session.exec(
                    select(func.count())
                    .select_from(UserFollow)
                    .where(
                        col(UserFollow.target_mid) == mid,
                        col(UserFollow.status) == FollowStatusEnum.FOLLOWING,
                    )
                )
            ).one()
            or 0
        )
        rows = (
            await session.exec(
                select(UserFollow)
                .where(
                    col(UserFollow.target_mid) == mid,
                    col(UserFollow.status) == FollowStatusEnum.FOLLOWING,
                )
                .order_by(col(UserFollow.created_at).desc())
                .offset((page_num - 1) * page_size)
                .limit(page_size)
            )
        ).all()

        follower_mids = [r.mid for r in rows]
        # mutual_set: 我也关注了的粉丝（即互相关注）
        # 注意 _mutual_mids 的语义是「mid 关注这些人 且 这些人也关注 mid」，
        # 对于「我的粉丝」场景，传入粉丝列表即可（双向对称，结果一致）
        mutual_set = await FollowService._mutual_mids(session, mid, follower_mids)

        items = [
            FollowListItem(
                mid=r.mid,
                created_at=r.created_at,
                mutual=r.mid in mutual_set,
            )
            for r in rows
        ]
        return FollowListResp(
            items=items, total=total, page_num=page_num, page_size=page_size
        )

    # ==================== 闸门判定（供其他模块调用）====================

    @staticmethod
    async def is_following(
        session: AsyncSession, mid: int, target_mid: int
    ) -> bool:
        """mid 是否关注了 target_mid。"""
        return (
            await FollowService._relation_status(session, mid, target_mid)
            == FollowStatusEnum.FOLLOWING
        )

    @staticmethod
    async def is_mutual(
        session: AsyncSession, mid_a: int, mid_b: int
    ) -> bool:
        """两个用户是否互相关注。"""
        forward = await FollowService._relation_status(session, mid_a, mid_b)
        if forward != FollowStatusEnum.FOLLOWING:
            return False
        reverse = await FollowService._relation_status(session, mid_b, mid_a)
        return reverse == FollowStatusEnum.FOLLOWING

    @staticmethod
    async def is_blocked_by(
        session: AsyncSession, mid: int, blocker: int
    ) -> bool:
        """mid 是否被 blocker 拉黑（用于私信 / 关注等入口的拦截）。

        等价于「blocker → mid 的关系是否为 blocked」。
        """
        return (
            await FollowService._relation_status(session, blocker, mid)
            == FollowStatusEnum.BLOCKED
        )

    # ==================== 内部工具 ====================

    @staticmethod
    async def _relation_status(
        session: AsyncSession, mid: int, target_mid: int
    ) -> FollowStatusEnum | None:
        """读取 mid → target_mid 的当前关系状态，无记录返回 None。"""
        row = (
            await session.exec(
                select(UserFollow).where(
                    col(UserFollow.mid) == mid,
                    col(UserFollow.target_mid) == target_mid,
                )
            )
        ).one_or_none()
        return row.status if row else None

    @staticmethod
    async def _mutual_mids(
        session: AsyncSession, mid: int, candidate_mids: list[int]
    ) -> set[int]:
        """从 candidate_mids 中筛出与 mid 互相关注的 mid 集合。

        双向语义：mid 关注 X 且 X 关注 mid。一次查询拿到「我关注的人」与
        「关注我的人」的交集，避免对每个候选 mid 都查一次数据库。
        """
        if not candidate_mids:
            return set()

        # 我关注的人中，与 candidate_mids 相交的部分（mid → X 为 following）
        forward_rows = (
            await session.exec(
                select(UserFollow.target_mid).where(
                    col(UserFollow.mid) == mid,
                    col(UserFollow.target_mid).in_(candidate_mids),
                    col(UserFollow.status) == FollowStatusEnum.FOLLOWING,
                )
            )
        ).all()
        forward_set = {r for r in forward_rows}  # type: ignore[union-attr]

        # 关注我的人中，与 forward_set 相交的部分（X → mid 为 following）
        if not forward_set:
            return set()
        reverse_rows = (
            await session.exec(
                select(UserFollow.mid).where(
                    col(UserFollow.target_mid) == mid,
                    col(UserFollow.mid).in_(forward_set),
                    col(UserFollow.status) == FollowStatusEnum.FOLLOWING,
                )
            )
        ).all()
        return {r for r in reverse_rows}  # type: ignore[union-attr]

    @staticmethod
    async def _build_op_resp(
        session: AsyncSession, mid: int, target_mid: int
    ) -> FollowOpResp:
        """根据操作完成后的当前关系构建回执。"""
        status = await FollowService._relation_status(session, mid, target_mid)
        return FollowOpResp(
            mid=mid,
            target_mid=target_mid,
            status=status,
            followed=status == FollowStatusEnum.FOLLOWING,
            blocked=status == FollowStatusEnum.BLOCKED,
        )


__all__ = ["FollowService"]
