"""用户封禁服务（审核联动）。

提供封禁 / 解封 / 列表 / 状态查询。封禁记录自包含在 `msg_user_ban`，
读取层实时计算是否到期：限时封禁 `banned_until` 早于当前时间即视为失效，
无需定时任务翻转；永久封禁 `banned_until` 为 `None` 恒生效。

封禁判定（`is_banned`）供评论 / 私信发布入口调用，实现「禁止违规用户使用对应服务」。
"""

from datetime import datetime

from loguru import logger
from sqlalchemy import cast, func, or_, String
from sqlmodel import col, select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.db.ban import UserBan
from app.models.enums import BanDurationTypeEnum, BanServiceEnum, BanStatusEnum
from app.models.schemas.ban import BanItem, BanListResp, BanServiceStatus, BanStatusResp


class BanService:
    """用户封禁读写。"""

    # 允许的服务集合（与 BanServiceEnum 对齐，用于入参校验）
    _VALID_SERVICES = {s.value for s in BanServiceEnum}

    @staticmethod
    def _service_like(service: str):
        """MySQL JSON 列不支持直接 LIKE，先 CAST 为字符串再模糊匹配。"""
        return cast(UserBan.ban_services, String).like(f'%"{service}"%')

    # ==================== 封禁 / 解封 ====================

    @staticmethod
    async def ban_users(
        session: AsyncSession,
        operator_mid: int,
        mids: list[int],
        ban_services: list[str],
        reason: str,
        duration_type: BanDurationTypeEnum,
        duration_days: int | None,
    ) -> int:
        """批量封禁用户（按服务维度 upsert）。

        同一用户同一服务的「生效中」旧记录先置为 `lifted`，再插入新记录，
        保证读取层取最新一条即可判定当前状态，且历史可追溯。

        返回本次新建的封禁记录条数。
        """
        # 入参校验：请求传小写 value（comment / dm），按枚举 value 归一化
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
            from datetime import timedelta

            banned_until = datetime.now() + timedelta(days=duration_days)
        else:
            duration_days = None
            banned_until = None

        # 先把命中用户的「生效中」旧记录 lifting（仅限本次要封的服务）
        await BanService._lift_active(
            session, mids, services, keep_status=BanStatusEnum.LIFTED
        )

        now = datetime.now()
        rows = [
            UserBan(
                mid=mid,
                ban_services=services,
                reason=reason,
                duration_type=duration_type,
                duration_days=duration_days,
                banned_until=banned_until,
                operator_mid=operator_mid,
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

    @staticmethod
    async def unban_users(
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
                # 仅保留未被解封的服务；若全部解封则整条 lifted
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

    @staticmethod
    async def _lift_active(
        session: AsyncSession,
        mids: list[int],
        services: list[str],
        keep_status: BanStatusEnum = BanStatusEnum.LIFTED,
    ) -> None:
        """把命中用户、命中服务且生效中的旧记录置为指定状态（默认 lifted）。

        用于封禁前的去重：避免同一用户同一服务出现多条 active 记录。
        JSON 数组字段用 `col(...).like` 模糊命中包含该服务的记录。
        """
        if not mids or not services:
            return
        or_conds = [BanService._service_like(s) for s in services]
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

    @staticmethod
    async def is_banned(
        session: AsyncSession, mid: int, service: str
    ) -> bool:
        """判定某用户在某服务上是否当前生效中（实时计算到期）。"""
        try:
            BanServiceEnum(service)
        except ValueError:
            return False
        now = datetime.now()
        rows = (
            await session.exec(
                select(UserBan)
                .where(col(UserBan.mid) == mid)
                .where(col(UserBan.status) == BanStatusEnum.ACTIVE)
                .where(BanService._service_like(service))
            )
        ).all()
        for r in rows:
            if r.banned_until is None:
                return True
            if r.banned_until > now:
                return True
        return False

    @staticmethod
    async def list_bans(
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

    @staticmethod
    async def get_status(
        session: AsyncSession, mid: int
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
            # 计算该条记录每个服务是否仍生效
            for svc in r.ban_services:
                try:
                    BanServiceEnum(svc)
                except ValueError:
                    continue
                still_active = r.status == BanStatusEnum.ACTIVE and (
                    r.banned_until is None or r.banned_until > now
                )
                # 仅保留「当前生效中」或「尚未记录过该服务」的明细
                existing = services.get(svc)
                if existing and existing.banned:
                    continue  # 已有一条更优的生效记录
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


__all__ = ["BanService"]
