"""pptr 账号领域对象（面向对象建模，base）。

把「一个 pptr 账号」建模为 :class:`PptrUser`：**构造只传一个 uid**，不要求完整用户信息；
涉及档案的读 / 写方法在调用时按需直连 pptr Postgres（四表联查）加载，
无需在构造期预取全量数据。权限以「集合」表达，支持运行时叠加多个权限。

具体能力：
- 信息读取：`fetch_profile` / `get_space_info` / `get_user_nav_data` /
  `get_many`（批量）/ `search_by_uname` / `search_users` / `list_act_log` / `list_exp_record`；
- 自身写操作：`set_user_detail` / `set_user_role` / `add_exp` / `add_daily_login_exp` /
  `add_username_record` / `update_user_info` / `create`（工厂）；
- 状态：`exists_active` / `is_banned`（按服务判定是否当前被封）/ `to_brief`；
- 按权限派生的治理写操作（封禁 / 解封 / 列表 / 状态汇总）见 `user_governance.UserGovernanceUser`。

用户主数据只有一份，就在 pptr 的 Postgres（PPTR_Bili_Lot）。本对象直连该库只读 / 写
用户主数据，与 pptr 侧 sequelize 的 `paranoid`（软删）默认行为对齐（读取统一过滤
`deletedAt IS NULL`）。封禁数据落 be-message 自有库（`msg_user_ban`），见治理子类。
"""

from __future__ import annotations

import datetime
import uuid
from typing import Iterable

from bili_common.exceptions import ResourceConflictException
from bili_common.models import (
    VALID_ROLES,
    PptrUserLevelInfo,
    PptrUserNavData,
    PptrUserRoleInfo,
    PptrUserSearchItem,
    PptrUserVipInfo,
)
from bili_common.deps.permissions import UserPermission
from loguru import logger
from sqlmodel import col, func, select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.core.config import settings
from app.core.database import new_pptr_session, new_session
from app.core.sharding import generate_uid
from app.models.enums import ExpActionType
from app.services.infrastructure.geo_ip import lookup as geo_lookup
from app.utils.ip_mask import mask_ipv4, mask_ipv6
from app.models.pptr_db import (
    PptrUserActInfoLog,
    PptrUserExpRecord,
    PptrUserNameRecord,
)
from app.models.pptr_user import (
    PptrUserDetail,
    PptrUserInfo,
    PptrUserLevel,
    PptrUserVip,
)
from app.models.schemas import (
    SpaceInfoResp,
    SpaceOfficial,
    SpaceVipWrap,
    UserActLogItem,
    UserActLogListResp,
    UserExpRecordItem,
    UserExpRecordListResp,
)
from app.models.schemas.user_brief import UserBriefOut


# ----------------------------------------------------------------------------
# 模块级工具函数（经验算法 / 脱敏 / 四表联查构造 / 昵称唯一校验 / upsert）
# ----------------------------------------------------------------------------


def _mask_email(email: str | None) -> str | None:
    """邮箱脱敏：保留 @ 前 3 位与完整域名，中间以 * 填充。"""
    if not email or "@" not in email:
        return email
    local, domain = email.split("@", 1)
    if len(local) <= 3:
        return f"{local[:1]}***{domain}"
    star_count = max(3, len(local) - 5)
    return f"{local[:3]}{'*' * star_count}{domain}"


def _mask_ip(ip: str) -> str:
    """IP 脱敏：按协议打码，IPv4→`a.b.*.*`，IPv6→`a:b:*`；空/非法回退空串。"""
    if not ip:
        return ""
    v4 = mask_ipv4(ip)
    if v4 is not None:
        return v4
    v6 = mask_ipv6(ip)
    if v6 is not None:
        return v6
    return ""


def _level_calc(current_exp: int, uid: int = 0) -> PptrUserLevelInfo:
    """根据累积经验复刻 pptr `level_calc`，输出等级 / 起止经验 / 下一级所需经验。"""
    exp = int(current_exp or 0)
    max_level = settings.level_max_level
    reqs = settings.level_exp_requirements
    current_level = 0
    current_min = 0
    for level in range(1, max_level + 1):
        required = reqs.get(level, 0)
        if exp >= required:
            current_level = level
            current_min = required
        else:
            break
    if current_level < max_level:
        next_exp = max(0, reqs.get(current_level + 1, 0) - exp)
    else:
        next_exp = 0
    return PptrUserLevelInfo(
        uid=int(uid),
        current_level=current_level,
        current_exp=exp,
        current_min=current_min,
        next_exp=next_exp,
    )


def _build_brief(
    info: PptrUserInfo,
    detail: PptrUserDetail | None,
    vip: PptrUserVip | None,
    level: PptrUserLevel | None,
) -> UserBriefOut:
    """把 pptr 四张表的一行拼成**完整**用户简档（含私密字段，仅供本人 / 管理员视角）。

    私域字段（脱敏邮箱 / 经验 / 大会员到期 / 角色）带 ``Private()`` 标记，
    在序列化期由 ``VisibilityMixin`` 按访问者身份自动剥离，对外展示路径
    （评论 / 私信 / 黑名单 / @ 面板）无需额外处理。
    """
    uname = detail.uname if (detail and detail.uname) else info.user_name
    level_value = 0
    if level is not None and level.current_level is not None:
        level_value = int(level.current_level)
    vip_status = None
    vip_type = 0
    vip_due_date = None
    if vip is not None:
        if vip.vip_status is not None:
            vip_status = str(vip.vip_status)
        if vip.vip_type is not None:
            vip_type = int(vip.vip_type)
        if vip.vip_due_date is not None:
            vip_due_date = int(vip.vip_due_date)
    return UserBriefOut(
        mid=int(info.uid),
        uname=uname or None,
        avatar=detail.avatar if detail else None,
        level=level_value,
        vip_status=vip_status,
        vip_type=vip_type,
        vip_due_date=vip_due_date,
        sex=detail.sex if detail else None,
        sign=detail.sign if detail else None,
        exp=int(level.current_exp) if level and level.current_exp is not None else None,
        role=info.role or None,
        email=_mask_email(detail.email) if detail and detail.email else None,
    )


def _base_select():
    """构造「TUserInfo 左联 detail/vip/level」的只读查询，均按软删过滤。"""
    return (
        select(PptrUserInfo, PptrUserDetail, PptrUserVip, PptrUserLevel)
        .outerjoin(
            PptrUserDetail,
            (col(PptrUserDetail.mid) == col(PptrUserInfo.uid))
            & col(PptrUserDetail.deletedAt).is_(None),
        )
        .outerjoin(
            PptrUserVip,
            (col(PptrUserVip.mid) == col(PptrUserInfo.uid))
            & col(PptrUserVip.deletedAt).is_(None),
        )
        .outerjoin(
            PptrUserLevel,
            (col(PptrUserLevel.mid) == col(PptrUserInfo.uid))
            & col(PptrUserLevel.deletedAt).is_(None),
        )
    )


async def _uname_taken(
    uname: str, *, self_uid: int | None = None, session: AsyncSession | None = None
) -> bool:
    """判断昵称 `uname` 是否已被其他用户占用（对齐 B 站昵称唯一规则）。"""
    if not uname:
        return False

    async def _run(s: AsyncSession) -> bool:
        stmt = select(PptrUserDetail.mid).where(PptrUserDetail.uname == str(uname))
        rows = (await s.exec(stmt)).all()
        for mid in rows:
            if self_uid is None or int(mid) != int(self_uid):
                return True
        return False

    if session is not None:
        return await _run(session)
    async with new_pptr_session() as s:
        return await _run(s)


async def _upsert_profile(
    s: AsyncSession, uid: int, detail, level, vip
) -> None:
    """按 uid（mid）upsert TUserDetail / TUserLevel / TUserVip（不触碰 TUserInfo.pwd）。"""
    existing = await s.exec(select(PptrUserDetail).where(PptrUserDetail.mid == uid))
    d = existing.first()
    if d is None:
        detail.mid = uid
        s.add(detail)
        await s.flush()
    else:
        if not (d.uname or "") and detail.uname is not None:
            d.uname = detail.uname
        if detail.avatar is not None:
            d.avatar = detail.avatar
        if detail.sign is not None:
            d.sign = detail.sign
        if detail.sex is not None:
            d.sex = detail.sex
        if detail.email is not None:
            d.email = detail.email
        if detail.birthday is not None:
            d.birthday = detail.birthday
        s.add(d)

    lv = (await s.exec(select(PptrUserLevel).where(PptrUserLevel.mid == uid))).first()
    if lv is None:
        level.mid = uid
        s.add(level)
    else:
        if level.current_level:
            lv.current_level = level.current_level
        s.add(lv)

    vp = (await s.exec(select(PptrUserVip).where(PptrUserVip.mid == uid))).first()
    if vp is None:
        vip.mid = uid
        s.add(vip)
    else:
        if vip.vip_type:
            vp.vip_type = vip.vip_type
        if vip.vip_due_date:
            vp.vip_due_date = vip.vip_due_date
        if vip.vip_status:
            vp.vip_status = vip.vip_status
        s.add(vp)


def _service_like(service: str):
    """MySQL JSON 列不支持直接 LIKE，先 CAST 为字符串再模糊匹配。"""
    from sqlalchemy import String, cast

    from app.models.db.ban_tbl import UserBan

    return cast(UserBan.ban_services, String).like(f'%"{service}"%')


# ----------------------------------------------------------------------------
# 账号领域对象
# ----------------------------------------------------------------------------


class PptrUser:
    """pptr 账号基础对象（所有登录用户）。

    构造只需一个 ``mid``；档案相关方法在调用时按需加载（懒加载），
    不预取完整用户信息。权限以「集合」表达，支持运行时叠加多个权限。
    """

    # 角色预设权限：基础用户为空（仅能操作自己的数据）
    ROLE_PERMISSIONS: frozenset[UserPermission] = frozenset()

    def __init__(
        self,
        *,
        mid: int,
        permissions: Iterable[UserPermission] | None = None,
    ) -> None:
        self.mid = int(mid)
        # 账号状态：行存在且未被软删/注销（注销为物理删除，故不存在即视为停用）。
        # 构造期不做 DB 校验，状态以 `exists_active` / `load` 的返回为准；
        # 这里给一个乐观默认值，供轻量判定使用。
        self.is_active = True
        # 权限 = 角色预设 ∪ 显式赋予（一个账号可叠加多个权限）
        self.permissions: set[UserPermission] = set(self.ROLE_PERMISSIONS)
        if permissions:
            self.permissions.update(permissions)

    # -------------------------- 权限 --------------------------

    def has_permission(self, perm: UserPermission) -> bool:
        return perm in self.permissions

    def has_any_permission(self, *perms: UserPermission) -> bool:
        return any(self.has_permission(p) for p in perms)

    @property
    def is_deactivated(self) -> bool:
        """是否已停用（不存在 / 已软删 / 已注销）。"""
        return not self.is_active

    # -------------------------- 档案读取 --------------------------

    @classmethod
    async def fetch_profile(
        cls,
        *,
        uid: int | None = None,
        user_name: str | None = None,
        session: AsyncSession | None = None,
    ) -> (
        tuple[
            PptrUserInfo,
            PptrUserDetail | None,
            PptrUserVip | None,
            PptrUserLevel | None,
        ]
        | None
    ):
        """按 uid 或 user_name 取齐四张表的一行原始记录，不存在返回 None。"""
        if uid:
            cond = col(PptrUserInfo.uid) == int(uid)
        elif user_name:
            cond = col(PptrUserInfo.user_name) == str(user_name)
        else:
            return None
        stmt = _base_select().where(cond, col(PptrUserInfo.deletedAt).is_(None))
        if session is not None:
            return (await session.exec(stmt)).first()
        async with new_pptr_session() as s:
            return (await s.exec(stmt)).first()

    async def get_profile(
        self, *, session: AsyncSession | None = None
    ) -> (
        tuple[
            PptrUserInfo,
            PptrUserDetail | None,
            PptrUserVip | None,
            PptrUserLevel | None,
        ]
        | None
    ):
        """加载本账号的四表档案（懒加载入口）。"""
        return await self.fetch_profile(uid=self.mid, session=session)

    async def get_space_info(
        self, *, session: AsyncSession | None = None
    ) -> SpaceInfoResp | None:
        """构建单个用户的完整空间资料（对标 B 站 `/x/space/wbi/acc/info`）。"""
        profile = await self.get_profile(session=session)
        if profile is None:
            return None
        info, detail, vip, level = profile

        vip_type = int(vip.vip_type) if vip and vip.vip_type is not None else 0
        vip_status = int(vip.vip_status) if vip and vip.vip_status is not None else 0
        vip_due_date = (
            int(vip.vip_due_date) if vip and vip.vip_due_date is not None else 0
        )

        birthday = ""
        if detail and detail.birthday:
            try:
                birthday = detail.birthday.strftime("%m-%d")
            except (AttributeError, ValueError):
                birthday = ""

        return SpaceInfoResp(
            mid=int(info.uid),
            name=(detail.uname if detail and detail.uname else info.user_name) or "",
            sex=(detail.sex if detail else "") or "保密",
            face=detail.avatar if detail else None,
            sign=(detail.sign if detail else "") or "",
            level=(
                int(level.current_level)
                if level and level.current_level is not None
                else 0
            ),
            birthday=birthday,
            vip=SpaceVipWrap(
                type=vip_type,
                status=vip_status,
                due_date=vip_due_date,
            ),
            official=SpaceOfficial(),
            pendant=None,
            nameplate=None,
            top_photo=None,
            is_followed=False,
            is_self=False,
        )

    async def get_user_nav_data(
        self, *, ip: str = "", ua: str = "", session: AsyncSession | None = None
    ) -> PptrUserNavData | None:
        """一次调用拿齐导航栏全部数据（含每日登录加经验、等级计算、邮件脱敏）。"""
        exp_result = await self.add_daily_login_exp()
        if exp_result.get("can_add_exp"):
            try:
                async with new_pptr_session() as s:
                    s.add(
                        PptrUserActInfoLog(
                            mid=self.mid,
                            ip=ip or "",
                            ua=ua or "",
                            headers={},
                            act_info="daily_login",
                        )
                    )
                    await s.commit()
            except Exception:  # noqa: BLE001
                logger.warning(
                    f"[nav] 用户 {self.mid} 记录每日首次登录行为失败（不影响 nav 返回）"
                )

        async def _run(s: AsyncSession) -> PptrUserNavData | None:
            row = (
                await s.exec(
                    _base_select().where(col(PptrUserInfo.uid) == self.mid)
                )
            ).first()
            if row is None:
                return None
            info, detail, _vip, level = row
            role = (info.role or "level0").lower()
            lv_info = _level_calc(level.current_exp if level else 0, uid=self.mid)
            return PptrUserNavData(
                uid=str(info.uid),
                user_name=(
                    detail.uname if (detail and detail.uname) else info.user_name
                )
                or "",
                role_info=PptrUserRoleInfo(
                    role_name=role,
                    role_description=settings.level_role_description.get(
                        role, "普通用户 (Lv0)"
                    ),
                ),
                face=detail.avatar if detail else None,
                level_info=lv_info,
                email=_mask_email(detail.email) if (detail and detail.email) else None,
            )

        if session is not None:
            return await _run(session)
        async with new_pptr_session() as s:
            return await _run(s)

    @classmethod
    async def get_many(
        cls,
        mids: list[int] | set[int],
        *,
        session: AsyncSession | None = None,
    ) -> dict[int, UserBriefOut]:
        """批量取用户完整简档，返回 `mid -> 用户信息` 字典（评论 / 私信列表装配核心）。

        返回完整简档（含脱敏邮箱 / 经验 / 大会员到期 / 角色等私域字段）。
        私域字段在序列化期按访问者身份自动剥离，仅本人 / 管理员可见。
        """
        unique = {int(m) for m in mids if m}
        if not unique:
            return {}

        stmt = _base_select().where(
            col(PptrUserInfo.uid).in_(unique),
            col(PptrUserInfo.deletedAt).is_(None),
        )

        async def _run(s: AsyncSession) -> dict[int, UserBriefOut]:
            rows = (await s.exec(stmt)).all()
            return {
                int(info.uid): _build_brief(info, detail, vip, level)
                for info, detail, vip, level in rows
            }

        if session is not None:
            return await _run(session)
        async with new_pptr_session() as s:
            return await _run(s)

    @classmethod
    async def search_by_uname(
        cls,
        keyword: str,
        limit: int = 10,
        *,
        session: AsyncSession | None = None,
    ) -> list[UserBriefOut]:
        """按昵称 / 注册名模糊搜索用户（@ 面板用）。"""
        keyword = (keyword or "").strip()
        if not keyword:
            return []
        escaped = keyword.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        pattern = f"%{escaped}%"

        stmt = (
            _base_select()
            .where(
                col(PptrUserInfo.deletedAt).is_(None),
                col(PptrUserDetail.uname).ilike(pattern)
                | col(PptrUserInfo.user_name).ilike(pattern),
            )
            .order_by(col(PptrUserInfo.uid))
            .limit(limit)
        )

        async def _run(s: AsyncSession) -> list[UserBriefOut]:
            rows = (await s.exec(stmt)).all()
            return [
                _build_brief(info, detail, vip, level)
                for info, detail, vip, level in rows
            ]

        if session is not None:
            result = await _run(session)
        else:
            async with new_pptr_session() as s:
                result = await _run(s)
        logger.debug(f"@用户搜索 keyword={keyword} 命中 {len(result)} 条")
        return result

    @classmethod
    async def search_users(
        cls,
        keyword: str,
        offset: int = 0,
        limit: int = 20,
        *,
        session: AsyncSession | None = None,
    ) -> tuple[list[PptrUserSearchItem], bool]:
        """管理端用户搜索（对齐 pptr `GET /api/v1/user/search` 的返回结构）。"""
        keyword = (keyword or "").strip()
        if not keyword:
            return [], False
        offset = max(0, int(offset))
        limit = max(1, min(int(limit), 100))

        mid_match = None
        if keyword.isdigit():
            try:
                mid_val = int(keyword)
                mid_match = col(PptrUserInfo.uid) == mid_val
            except ValueError:
                mid_match = None

        escaped = keyword.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        pattern = f"%{escaped}%"
        fuzzy = (
            col(PptrUserDetail.uname).ilike(pattern)
            | col(PptrUserInfo.user_name).ilike(pattern)
            | col(PptrUserDetail.email).ilike(pattern)
        )
        where_clause = col(PptrUserInfo.deletedAt).is_(None)
        if mid_match is not None:
            where_clause = where_clause & (mid_match | fuzzy)
        else:
            where_clause = where_clause & fuzzy

        stmt = (
            _base_select()
            .where(where_clause)
            .order_by(col(PptrUserInfo.uid))
            .offset(offset)
            .limit(limit)
        )

        async def _run(s: AsyncSession) -> tuple[list[PptrUserSearchItem], bool]:
            rows = (await s.exec(stmt)).all()
            items: list[PptrUserSearchItem] = []
            for info, detail, vip, level in rows:
                mid = int(info.uid)
                uname = detail.uname if (detail and detail.uname) else info.user_name
                role = (info.role or "level0").lower()
                role_desc = settings.level_role_description.get(role, "普通用户 (Lv0)")
                level_info = _level_calc(level.current_exp if level else 0)
                vip_info = PptrUserVipInfo(
                    vip_status=(
                        int(vip.vip_status) if vip and vip.vip_status is not None else 0
                    ),
                    vip_type=(
                        int(vip.vip_type) if vip and vip.vip_type is not None else 0
                    ),
                    vip_due_date=(
                        int(vip.vip_due_date)
                        if vip and vip.vip_due_date is not None
                        else 0
                    ),
                    vip_pay_type=(
                        int(vip.vip_pay_type)
                        if vip and vip.vip_pay_type is not None
                        else 0
                    ),
                )
                role_info = PptrUserRoleInfo(
                    role_name=role, role_description=role_desc
                )
                items.append(
                    PptrUserSearchItem(
                        mid=str(mid),
                        uid=str(mid),
                        user_name=info.user_name,
                        uname=uname,
                        email=_mask_email(detail.email) if detail else None,
                        avatar=detail.avatar if detail else None,
                        sign=detail.sign if detail else None,
                        sex=detail.sex if detail else None,
                        regtime=(
                            int(info.createdAt.timestamp() * 1000)
                            if info.createdAt
                            else None
                        ),
                        level_info=level_info,
                        vip=vip_info,
                        role_info=role_info,
                    )
                )
            has_more = len(items) >= limit
            return items, has_more

        if session is not None:
            result = await _run(session)
        else:
            async with new_pptr_session() as s:
                result = await _run(s)
        items, has_more = result
        logger.debug(
            f"管理端用户搜索 keyword={keyword} offset={offset} limit={limit} 命中 {len(items)} 条, has_more={has_more}"
        )
        return result

    # -------------------------- 状态 / 简卡 --------------------------

    @classmethod
    async def exists_active(cls, mid: int) -> bool:
        """轻量校验：账号是否存在且未被软删/注销（仅查 TUserInfo 主键）。"""
        if not mid or int(mid) <= 0:
            return False
        async with new_pptr_session() as s:
            row = (
                await s.exec(
                    select(PptrUserInfo.uid)
                    .where(PptrUserInfo.uid == int(mid))
                    .where(col(PptrUserInfo.deletedAt).is_(None))
                )
            ).first()
        return row is not None

    @classmethod
    async def load(cls, mid: int) -> "PptrUser | None":
        """按 uid 加载账号对象；不存在或已注销返回 None。"""
        if not await cls.exists_active(mid):
            return None
        return cls(mid=int(mid))

    async def to_brief(self) -> UserBriefOut:
        """装配评论 / 私信列表用的用户简卡（私域字段由序列化期按访问者裁剪）。"""
        profile = await self.get_profile()
        if profile is None:
            return UserBriefOut(mid=self.mid)
        info, detail, vip, level = profile
        return _build_brief(info, detail, vip, level)

    # -------------------------- 自身写操作 --------------------------

    @classmethod
    async def create(
        cls,
        *,
        uid: int = 0,
        user_name: str,
        pwd: str = "",
        createdAt: str | None = None,
        uname: str = "",
        face: str | None = None,
        sign: str = "",
        sex: str = "保密",
        email: str = "",
        birthday: str = "",
        current_level: int = 0,
        vip_type: int = 0,
        vip_due_date: int = 0,
        vip_status: int = 0,
        ip: str | None = None,
        ua: str | None = None,
        session: AsyncSession | None = None,
    ) -> tuple[int, bool]:
        """创建用户（一次性写入 TUserInfo + TUserDetail + TUserLevel + TUserVip）。"""
        parsed_created = None
        if createdAt:
            try:
                parsed_created = datetime.datetime.fromisoformat(
                    createdAt.replace("Z", "+00:00")
                )
            except (ValueError, TypeError):
                parsed_created = None

        parsed_birthday = None
        if birthday:
            try:
                parsed_birthday = datetime.datetime.fromisoformat(
                    birthday.replace("Z", "+00:00")
                )
            except (ValueError, TypeError):
                parsed_birthday = None

        _uid = uid if uid else await generate_uid()

        info = PptrUserInfo(
            uid=_uid,
            user_name=user_name,
            pwd=pwd,
            createdAt=parsed_created,
            role="level0",
        )
        detail = PptrUserDetail(
            mid=_uid,
            uname=uname or None,
            avatar=face or None,
            sign=sign or None,
            sex=sex or None,
            email=email or None,
            birthday=parsed_birthday,
        )
        level = PptrUserLevel(
            mid=_uid, current_level=current_level, current_exp=0, current_min=0
        )
        vip = PptrUserVip(
            mid=_uid,
            vip_type=vip_type,
            vip_due_date=vip_due_date,
            vip_status=vip_status,
            vip_pay_type=0,
        )

        async def _run(s: AsyncSession) -> tuple[int, bool]:
            exists_uid = await s.scalar(
                func.min(PptrUserInfo.uid)
                .select()
                .select_from(PptrUserInfo)
                .where(PptrUserInfo.user_name == user_name)
            )
            if exists_uid is not None:
                real_uid = int(exists_uid)
                await _upsert_profile(s, real_uid, detail, level, vip)
                await s.commit()
                return real_uid, False
            if uname and await _uname_taken(uname, session=s):
                detail.uname = f"bili_{uuid.uuid4().hex[:12]}"
            s.add(info)
            await s.flush()
            s.add(detail)
            await s.flush()
            s.add(level)
            s.add(vip)
            if ip:
                act_log = PptrUserActInfoLog(
                    mid=int(info.uid),
                    ip=ip,
                    ua=ua or "",
                    headers={},
                    act_info="reg",
                )
                s.add(act_log)
                await s.flush()
                info.reg_ip_info_id = act_log.pk
                await s.flush()
            await s.commit()
            await s.refresh(info)
            return int(info.uid), True

        if session is not None:
            return await _run(session)
        async with new_pptr_session() as s:
            return await _run(s)

    async def update_user_info(
        self,
        *,
        user_name: str = "",
        pwd: str | None = None,
        reg_ip_info_id: int = 0,
        session: AsyncSession | None = None,
    ) -> tuple[int, bool]:
        """更新用户（按 uid 更新 TUserInfo.pwd / reg_ip_info_id）。"""
        stmt = select(PptrUserInfo).where(PptrUserInfo.uid == self.mid)
        if user_name:
            stmt = stmt.where(PptrUserInfo.user_name == str(user_name))

        async def _run(s: AsyncSession) -> tuple[int, bool]:
            info = (await s.exec(stmt)).first()
            if not info:
                return 0, False
            if pwd is not None and pwd != "":
                info.pwd = pwd
            if reg_ip_info_id:
                info.reg_ip_info_id = reg_ip_info_id
            s.add(info)
            await s.commit()
            return int(info.uid), True

        if session is not None:
            return await _run(session)
        async with new_pptr_session() as s:
            return await _run(s)

    async def get_user_level(self) -> tuple[int, int, int, str] | None:
        """按本账号 uid 取 TUserLevel。"""
        async with new_pptr_session() as s:
            lv = (
                await s.exec(
                    select(PptrUserLevel).where(PptrUserLevel.mid == self.mid)
                )
            ).one_or_none()
            if not lv:
                return None
            _updated_at = lv.updatedAt
            updated_at = _updated_at.isoformat() if _updated_at else ""
            return (
                int(lv.current_level or 0),
                int(lv.current_exp or 0),
                int(lv.current_min or 0),
                updated_at,
            )

    async def set_user_level(
        self, *, current_level: int, current_exp: int, current_min: int
    ) -> bool:
        """原子写入 TUserLevel。"""
        async with new_pptr_session() as s:
            lv = (
                await s.exec(
                    select(PptrUserLevel).where(PptrUserLevel.mid == self.mid)
                )
            ).first()
            if lv is None:
                s.add(
                    PptrUserLevel(
                        mid=self.mid,
                        current_level=current_level,
                        current_exp=current_exp,
                        current_min=current_min,
                    )
                )
            else:
                lv.current_level = current_level
                lv.current_exp = current_exp
                lv.current_min = current_min
                s.add(lv)
            await s.commit()
            return True

    async def set_user_detail(
        self,
        *,
        uname: str = "",
        face: str | None = None,
        sign: str = "",
        sex: str = "保密",
        email: str = "",
        birthday: str = "",
    ) -> bool:
        """更新本账号 TUserDetail。"""
        parsed_birthday = None
        if birthday:
            try:
                parsed_birthday = datetime.datetime.fromisoformat(
                    birthday.replace("Z", "+00:00")
                )
            except (ValueError, TypeError):
                parsed_birthday = None

        async with new_pptr_session() as s:
            d = (
                await s.exec(
                    select(PptrUserDetail).where(PptrUserDetail.mid == self.mid)
                )
            ).first()
            if d is None:
                parent = (
                    await s.exec(
                        select(PptrUserInfo.uid).where(
                            PptrUserInfo.uid == self.mid
                        )
                    )
                ).first()
                if parent is None:
                    raise ValueError(
                        f"用户不存在 uid={self.mid}，无法写入公开资料"
                    )
                if uname and await _uname_taken(
                    uname, self_uid=self.mid, session=s
                ):
                    raise ResourceConflictException("昵称已被占用，请更换昵称后重试")
                s.add(
                    PptrUserDetail(
                        mid=self.mid,
                        uname=uname or None,
                        avatar=face or None,
                        sign=sign or None,
                        sex=sex or None,
                        email=email or None,
                        birthday=parsed_birthday,
                    )
                )
            else:
                if uname and uname != (d.uname or ""):
                    if await _uname_taken(uname, self_uid=self.mid, session=s):
                        raise ResourceConflictException(
                            "昵称已被占用，请更换昵称后重试"
                        )
                if uname:
                    d.uname = uname
                if face:
                    d.avatar = face
                if sign:
                    d.sign = sign
                if sex:
                    d.sex = sex
                if email:
                    d.email = email
                if parsed_birthday is not None:
                    d.birthday = parsed_birthday
                s.add(d)
            await s.commit()
            return True

    async def set_user_role(self, *, role: str) -> bool:
        """更新本账号 TUserInfo.role。"""
        if role not in VALID_ROLES:
            return False
        async with new_pptr_session() as s:
            info = (
                await s.exec(
                    select(PptrUserInfo).where(PptrUserInfo.uid == self.mid)
                )
            ).first()
            if not info:
                return False
            if info.role == "root" and role != "root":
                return False
            info.role = role
            s.add(info)
            await s.commit()
            return True

    @staticmethod
    def _level_to_role(level: int) -> str:
        """由纯数字等级（0~max）生成成长等级角色。"""
        lv = int(level)
        if 0 <= lv <= settings.level_max_level:
            return f"level{lv}"
        return "level0"

    async def sync_role_on_level_up(self, *, new_level: int) -> bool:
        """升级时自动同步成长等级角色（不覆盖 root）。"""
        async with new_pptr_session() as s:
            info = (
                await s.exec(
                    select(PptrUserInfo).where(PptrUserInfo.uid == self.mid)
                )
            ).first()
            if not info:
                return False
            if info.role == "root":
                return False
            target_role = self._level_to_role(new_level)
            if info.role == target_role:
                return False
            info.role = target_role
            s.add(info)
            await s.commit()
            return True

    async def add_exp(self, *, exp: int, action_type: str = "") -> dict:
        """增加经验值（业务逻辑整体在 be-message 完成，pptr 仅传 uid/exp）。"""
        level_row = await self.get_user_level()
        if level_row is None:
            old_level, old_exp = 0, 0
        else:
            old_level, old_exp, _, _ = level_row

        new_exp = int(old_exp) + int(exp)
        calc = _level_calc(new_exp, uid=self.mid)
        new_level = calc.current_level

        await self.set_user_level(
            current_level=calc.current_level,
            current_exp=calc.current_exp,
            current_min=calc.current_min,
        )

        if action_type:
            today_str = datetime.datetime.now().astimezone().strftime("%Y-%m-%d")
            try:
                at = ExpActionType[action_type.upper()].value
            except (KeyError, AttributeError):
                at = int(action_type)
            async with new_pptr_session() as s:
                s.add(
                    PptrUserExpRecord(
                        mid=self.mid,
                        action_type=at,
                        exp=int(exp),
                        ref_date=today_str,
                    )
                )
                await s.commit()

        role_updated = False
        if new_level > old_level:
            role_updated = await self.sync_role_on_level_up(new_level=new_level)

        return {
            "uid": self.mid,
            "old_exp": int(old_exp),
            "new_exp": int(new_exp),
            "old_level": int(old_level),
            "new_level": int(new_level),
            "leveled_up": new_level > old_level,
            "role_updated": role_updated,
        }

    async def add_daily_login_exp(self) -> dict:
        """每日首次登录加经验（基于 TUserExpRecord 做每日幂等检查）。"""
        today_str = datetime.datetime.now().astimezone().strftime("%Y-%m-%d")

        async with new_pptr_session() as s:
            existing = (
                await s.exec(
                    select(PptrUserExpRecord).where(
                        PptrUserExpRecord.mid == self.mid,
                        PptrUserExpRecord.action_type
                        == ExpActionType.DAILY_LOGIN.value,
                        PptrUserExpRecord.ref_date == today_str,
                    )
                )
            ).first()

        if existing is not None:
            level_row = await self.get_user_level()
            if level_row is None:
                old_level, old_exp = 0, 0
            else:
                old_level, old_exp, _, _ = level_row
            calc = _level_calc(old_exp, uid=self.mid)
            return {
                "uid": self.mid,
                "can_add_exp": False,
                "old_exp": int(old_exp),
                "new_exp": int(old_exp),
                "old_level": int(old_level),
                "new_level": int(old_level),
                "leveled_up": False,
                "role_updated": False,
                "level_info": calc,
            }

        level_row = await self.get_user_level()
        if level_row is None:
            old_level = 0
            old_exp = 0
        else:
            old_level, old_exp, _, _ = level_row

        daily_exp = settings.level_daily_exp_bonus
        new_exp = int(old_exp) + int(daily_exp)
        calc = _level_calc(new_exp, uid=self.mid)
        new_level = calc.current_level

        await self.set_user_level(
            current_level=calc.current_level,
            current_exp=calc.current_exp,
            current_min=calc.current_min,
        )

        async with new_pptr_session() as s:
            s.add(
                PptrUserExpRecord(
                    mid=self.mid,
                    action_type=ExpActionType.DAILY_LOGIN.value,
                    exp=int(daily_exp),
                    ref_date=today_str,
                )
            )
            await s.commit()

        role_updated = False
        if new_level > old_level:
            role_updated = await self.sync_role_on_level_up(new_level=new_level)

        return {
            "uid": self.mid,
            "can_add_exp": True,
            "old_exp": int(old_exp),
            "new_exp": int(new_exp),
            "old_level": int(old_level),
            "new_level": int(new_level),
            "leveled_up": new_level > old_level,
            "role_updated": role_updated,
            "level_info": calc,
        }

    async def add_username_record(self, *, prev_uname: str | None) -> bool:
        """记录昵称变更历史到 TUserNameRecord。"""
        async with new_pptr_session() as s:
            s.add(PptrUserNameRecord(mid=self.mid, prev_uname=prev_uname))
            await s.commit()
        return True

    # -------------------------- 用户中心「我的记录」 --------------------------

    async def list_act_log(
        self, offset: int = 0, limit: int = 10, days: int = 7
    ) -> UserActLogListResp:
        """查询本账号最近 `days` 天的登录 / 行为记录（脱敏）。"""
        offset = max(0, int(offset))
        limit = max(1, min(int(limit), 100))
        days = max(1, int(days))
        since = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(
            days=days
        )

        stmt = (
            select(PptrUserActInfoLog)
            .where(
                col(PptrUserActInfoLog.mid) == self.mid,
                PptrUserActInfoLog.createdAt >= since,
                col(PptrUserActInfoLog.deletedAt).is_(None),
            )
            .order_by(col(PptrUserActInfoLog.createdAt).desc())
            .offset(offset)
            .limit(limit + 1)
        )

        async with new_pptr_session() as s:
            rows = (await s.exec(stmt)).all()

        has_more = len(rows) > limit
        rows = rows[:limit]
        items = []
        for row in rows:
            raw_ip = row.ip or ""
            geo = geo_lookup(raw_ip)
            loc_parts = [p for p in (geo.poi, geo.isp) if p]
            location = " ".join(loc_parts) if loc_parts else "未知"
            items.append(
                UserActLogItem(
                    time=row.createdAt,
                    ip=_mask_ip(raw_ip),
                    location=location,
                    ua=(row.ua or ""),
                    act_info=(row.act_info or "login_succ"),
                )
            )
        return UserActLogListResp(items=items, has_more=has_more)

    async def list_exp_record(
        self, offset: int = 0, limit: int = 10, days: int = 7
    ) -> UserExpRecordListResp:
        """查询本账号最近 `days` 天的经验变动记录。"""
        offset = max(0, int(offset))
        limit = max(1, min(int(limit), 100))
        days = max(1, int(days))
        since = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(
            days=days
        )

        stmt = (
            select(PptrUserExpRecord)
            .where(
                col(PptrUserExpRecord.mid) == self.mid,
                PptrUserExpRecord.createdAt >= since,
            )
            .order_by(col(PptrUserExpRecord.createdAt).desc())
            .offset(offset)
            .limit(limit + 1)
        )

        async with new_pptr_session() as s:
            rows = (await s.exec(stmt)).all()

        has_more = len(rows) > limit
        rows = rows[:limit]
        items = []
        for row in rows:
            at = int(row.action_type or 0)
            try:
                action_name = ExpActionType(at).name.lower()
            except (ValueError, KeyError):
                action_name = ""
            items.append(
                UserExpRecordItem(
                    time=row.createdAt,
                    action_type=at,
                    action_name=action_name,
                    exp=int(row.exp or 0),
                    ref_date=row.ref_date or "",
                )
            )
        return UserExpRecordListResp(items=items, has_more=has_more)

    # -------------------------- 封禁状态（读取，任何账号适用） --------------------------

    async def is_banned(self, session: AsyncSession, service: str) -> bool:
        """判定本账号在某服务上是否当前生效中（实时计算到期）。"""
        from app.models.db.ban_tbl import UserBan
        from app.models.enums import BanServiceEnum, BanStatusEnum

        try:
            BanServiceEnum(service)
        except ValueError:
            return False
        now = datetime.datetime.now()
        rows = (
            await session.exec(
                select(UserBan)
                .where(col(UserBan.mid) == self.mid)
                .where(col(UserBan.status) == BanStatusEnum.ACTIVE)
                .where(_service_like(service))
            )
        ).all()
        for r in rows:
            if r.banned_until is None:
                return True
            if r.banned_until > now:
                return True
        return False

    # -------------------------- 注销 --------------------------

    async def deactivate(self) -> None:
        """注销当前账号：物理删除 pptr 四表 + 彻底清除 be-message 业务数据。

        注销 = **物理删除**（不可恢复）；Casdoor 不同步禁用。幂等：已注销 / 无数据时
        各 DELETE 影响 0 行仍正常返回。各业务「按 uid 彻底清除」逻辑内聚在
        `app.services.cleanup/` 的独立领域服务中，本方法仅按依赖顺序组合调用。
        """
        uid = self.mid
        if not uid or uid <= 0:
            raise ValueError("uid 不合法")
        await self._delete_pptr_user()
        await self._delete_message_data()
        logger.info(
            f"用户 {uid} 已注销（pptr 四表物理删除 + be-message 业务数据清除）"
        )

    async def _delete_pptr_user(self) -> None:
        """删除 pptr Postgres 四表 + 关联日志（单事务）。"""
        from app.services.cleanup import cleanup_pptr

        async with new_pptr_session() as s:
            await cleanup_pptr.CleanupPptrService.delete_all_by_uid(s, self.mid)
            await s.commit()

    async def _delete_message_data(self) -> None:
        """彻底清除 be-message MySQL 业务数据（单事务）。

        删除顺序：先清该用户的互动 / 关联，最后删其发布主体，避免 FK 无
        `ondelete` 级联时漏删（详见各 cleanup 服务 docstring）。
        """
        from app.services.cleanup import (
            cleanup_comment,
            cleanup_dm,
            cleanup_event,
            cleanup_favorite,
            cleanup_follow,
            cleanup_misc,
            cleanup_moment,
            cleanup_notify,
            cleanup_report,
        )

        cleanup_order = [
            cleanup_moment.CleanupMomentService,  # 动态（含点赞/浏览痕迹）
            cleanup_comment.CleanupCommentService,  # 评论
            cleanup_follow.CleanupFollowService,  # 关注 / 拉黑
            cleanup_report.CleanupReportService,  # 举报
            cleanup_favorite.CleanupFavoriteService,  # 收藏
            cleanup_dm.CleanupDmService,  # 私信
            cleanup_notify.CleanupNotifyService,  # 通知
            cleanup_event.CleanupEventService,  # 事件
            cleanup_misc.CleanupMiscService,  # 设置 / 活跃 / 封禁 / 管理
        ]
        async with new_session() as s:
            for svc in cleanup_order:
                await svc.delete_all_by_uid(s, self.mid)
            await s.commit()


__all__ = ["PptrUser"]
