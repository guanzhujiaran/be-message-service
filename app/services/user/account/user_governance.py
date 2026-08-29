"""用户治理角色对象（封禁 / 解封 / 封禁记录查询）。

封禁数据自包含在 be-message（`msg_user_ban`），与 pptr 用户主数据解耦。
本子类把 `app.services.admin.ban_service.BanService` 的具体方法搬入用户对象：
操作者即对象自身的 ``mid``（`operator_mid = self.mid`），被封禁目标作为方法入参传入。
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import func, or_
from sqlmodel import col, select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.db.ban_tbl import UserBan
from app.models.enums import BanDurationTypeEnum, BanServiceEnum, BanStatusEnum
from app.models.schemas.ban import BanItem, BanServiceStatus, BanStatusResp
from app.services.user.account.base import PptrUser, _service_like
from bili_common.deps.permissions import UserPermission


class UserGovernanceUser(PptrUser):
    """用户治理角色：封禁 / 解封（评论、私信、跨服务）+ 封禁记录查看。"""

    ROLE_PERMISSIONS = frozenset(
        {
            UserPermission.COMMENT_BAN,
            UserPermission.DM_BAN,
            UserPermission.USER_BAN,
            UserPermission.USER_BAN_VIEW,
        }
    )

    # ==================== 封禁 / 解封 ====================

    async def ban_users(
        self,
        session: AsyncSession,
        mids: list[int],
        ban_services: list[str],
        reason: str,
        duration_type: BanDurationTypeEnum,
        duration_days: int | None,
    ) -> int:
        """批量封禁用户（按服务维度 upsert）。操作者为本账号 `self.mid`。

        同一用户同一服务的「生效中」旧记录先置为 `lifted`，再插入新记录；
        返回本次新建的封禁记录条数。
        """
        services: list[str] = []
        for s in ban_services:
            try:
                services.append(BanServiceEnum(s).value)
            except ValueError:
                continue
        services = list(dict.fromkeys(services))
        if not services:
            return 0
        if duration_type == BanDurationTypeEnum.TEMPORARY:
            if not duration_days or duration_days < 1:
                duration_days = 1
            banned_until = datetime.now() + timedelta(days=duration_days)
        else:
            duration_days = None
            banned_until = None

        await self._lift_active(session, mids, services, keep_status=BanStatusEnum.LIFTED)

        now = datetime.now()
        rows = [
            UserBan(
                mid=mid,
                ban_services=services,
                reason=reason,
                duration_type=duration_type,
                duration_days=duration_days,
                banned_until=banned_until,
                operator_mid=self.mid,
                status=BanStatusEnum.ACTIVE,
                created_at=now,
                updated_at=now,
            )
            for mid in mids
            if mid > 0
        ]
        if not rows:
            return 0
        session.add_all(rows)
        await session.commit()
        return len(rows)

    async def unban_users(
        self,
        session: AsyncSession,
        mids: list[int],
        ban_services: list[str] | None = None,
    ) -> int:
        """批量解封：把命中用户的生效中记录置为 `lifted`。

        不传 `ban_services` 时解封该用户全部服务；传则只解除指定服务。
        返回被解除的记录条数。
        """
        if not mids:
            return 0
        stmt = (
            select(UserBan)
            .where(col(UserBan.mid).in_(mids))
            .where(col(UserBan.status) == BanStatusEnum.ACTIVE)
        )
        rows = (await session.exec(stmt)).all()
        if not rows:
            return 0

        lifted = 0
        for r in rows:
            if ban_services:
                remain = [s for s in r.ban_services if s not in ban_services]
                if remain:
                    r.ban_services = remain
                    session.add(r)
                    continue
            r.status = BanStatusEnum.LIFTED
            r.updated_at = datetime.now()
            session.add(r)
            lifted += 1
        await session.commit()
        return lifted

    async def _lift_active(
        self,
        session: AsyncSession,
        mids: list[int],
        services: list[str],
        keep_status: BanStatusEnum = BanStatusEnum.LIFTED,
    ) -> None:
        """把命中用户、命中服务且生效中的旧记录置为指定状态（默认 lifted）。"""
        if not mids or not services:
            return
        or_conds = [_service_like(s) for s in services]
        stmt = (
            select(UserBan)
            .where(col(UserBan.mid).in_(mids))
            .where(col(UserBan.status) == BanStatusEnum.ACTIVE)
            .where(or_(*or_conds))
        )
        rows = (await session.exec(stmt)).all()
        for r in rows:
            r.status = keep_status
            r.updated_at = datetime.now()
            session.add(r)
        if rows:
            await session.commit()

    # ==================== 查询 ====================

    async def list_bans(
        self,
        session: AsyncSession,
        status: BanStatusEnum | None = None,
        page_num: int = 1,
        page_size: int = 20,
    ) -> tuple[list[BanItem], int]:
        """封禁记录分页列表（按创建时间倒序）。"""
        conds = []
        if status is not None:
            conds.append(col(UserBan.status) == status)
        total = int(
            (
                await session.exec(
                    select(func.count())
                    .select_from(UserBan)
                    .where(*conds)
                )
            ).one()
            or 0
        )
        rows = (
            await session.exec(
                select(UserBan)
                .where(*conds)
                .order_by(col(UserBan.created_at).desc())
                .offset((page_num - 1) * page_size)
                .limit(page_size)
            )
        ).all()
        items = [
            BanItem(
                id=r.id,
                mid=r.mid,
                ban_services=r.ban_services,
                reason=r.reason,
                duration_type=r.duration_type,
                duration_days=r.duration_days,
                banned_until=r.banned_until,
                operator_mid=r.operator_mid,
                status=r.status,
                created_at=r.created_at,
                updated_at=r.updated_at,
            )
            for r in rows
        ]
        return items, total

    async def get_status(
        self,
        session: AsyncSession,
        mid: int,
    ) -> BanStatusResp:
        """汇总某用户在各服务的封禁状态（实时计算到期）。"""
        now = datetime.now()
        rows = (
            await session.exec(
                select(UserBan)
                .where(col(UserBan.mid) == mid)
                .order_by(col(UserBan.created_at).desc())
            )
        ).all()

        services: dict[str, BanServiceStatus] = {}
        for r in rows:
            for svc in r.ban_services:
                try:
                    BanServiceEnum(svc)
                except ValueError:
                    continue
                still_active = r.status == BanStatusEnum.ACTIVE and (
                    r.banned_until is None or r.banned_until > now
                )
                existing = services.get(svc)
                if existing and existing.banned:
                    continue
                services[svc] = BanServiceStatus(
                    banned=still_active,
                    status=r.status,
                    reason=r.reason,
                    duration_type=r.duration_type,
                    banned_until=r.banned_until,
                    operator_mid=r.operator_mid,
                )

        any_banned = any(s.banned for s in services.values())
        return BanStatusResp(mid=mid, banned=any_banned, services=services)


__all__ = ["UserGovernanceUser"]
