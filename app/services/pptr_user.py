"""pptr 用户信息只读服务。

**用户主数据只有一份，就在 pptr 的 Postgres（PPTR_Bili_Lot）。** 本服务直连该库
只读地取回评论 / 私信展示所需的用户信息（昵称 / 头像 / 等级 / 大会员 / 性别 / 签名），
以及 @ 面板的昵称搜索。**本服务不写该库**，也不在本地冗余任何用户快照。

为什么不再回调 pptr 的 HTTP 接口：一页 20 条评论涉及几十个 mid，逐个回调是典型 N+1；
直连 Postgres 一次 `WHERE uid IN (...)` 即可拿齐，且避免了服务间循环依赖
（网关 → be-message → pptr 接口 → be-message）。

一致性：pptr 的用户表均为 `paranoid`（软删），读取时统一过滤 `deletedAt IS NULL`，
与 pptr 侧 sequelize 的默认行为对齐。
"""

from fastapi import HTTPException
from loguru import logger
from sqlalchemy.exc import IntegrityError
from sqlmodel import col, select, func
from bili_common.exceptions import ResourceConflictException
from sqlmodel.ext.asyncio.session import AsyncSession
from app.core.database import new_pptr_session
from app.core.config import settings
from app.core.sharding import generate_uid
from app.models.pptr_user import (
    PptrUserDetail,
    PptrUserInfo,
    PptrUserLevel,
    PptrUserVip,
)
from app.models.enums import ExpActionType
from app.models.pptr_db import (
    PptrUserExpRecord,
    PptrUserNameRecord,
)
from bili_common.models import (
    PptrUserLevelInfo,
    PptrUserRoleInfo,
    PptrUserSearchItem,
    PptrUserVipInfo,
    VALID_ROLES,
    PptrUserNavData,
)
from app.models.schemas import CommentUserBrief
import datetime
import uuid
# 以下成长等级相关的配置（经验阈值 / 最大等级 / 角色文案）已全部下沉到 app.core.config
# 的 Settings，由环境变量驱动（对齐 pptr common_config.level_config 与 user_role_const）：
#   PPTR_LEVEL_MAX_LEVEL / PPTR_LEVEL_EXP_REQUIREMENTS / PPTR_LEVEL_ROLE_DESCRIPTION
# 业务代码统一从 `settings` 读取，不再在此处硬编码。


def _mask_user_name(user_name: str | None) -> str | None:
    """对注册默认名做「首尾保留、中间打码」的脱敏（用于昵称缺失时的兜底展示）。

    与 pptr `UserService.generate_user_detail_info` 的脱敏语义保持一致：
    取中间子串 `user_name[1:-1]`，把它在整个串中出现的位置全部替换为等长 `*`
    （即 JS `replaceAll(middle, '*'.repeat(len))` 的等价实现）。
    长度 < 2 时保持原样。
    """
    if not user_name:
        return user_name
    middle = user_name[1:-1]
    if not middle:
        return user_name
    return user_name.replace(middle, "*" * len(middle))


def _mask_email(email: str | None) -> str | None:
    """邮箱脱敏：保留 @ 前 3 位与完整域名，中间以 * 填充。

    对齐 pptr `Utl.mask_email`：local 长度 <= 3 时保留首字符；
    否则保留前 3 位，星号数取 max(3, len-5)。
    """
    if not email or "@" not in email:
        return email
    local, domain = email.split("@", 1)
    if len(local) <= 3:
        return f"{local[:1]}***{domain}"
    star_count = max(3, len(local) - 5)
    return f"{local[:3]}{'*' * star_count}{domain}"


def _level_calc(current_exp: int, uid: int = 0) -> PptrUserLevelInfo:
    """根据累积经验复刻 pptr `level_calc`，输出等级 / 起止经验 / 下一级所需经验。

    经验配置统一取自 settings.level_exp_requirements（已下沉 be-message）。
    """
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
        next_exp = reqs.get(current_level + 1, 0)
    else:
        next_exp = 0  # 满级无下一级
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
) -> CommentUserBrief:
    """把 pptr 四张表的一行拼成评论卡片用户信息。

    仅使用 pptr 现有四张表（TUserInfo/TUserDetail/TUserVip/TUserLevel）中**实际存在**的字段，
    不新增任何表结构或表外字段：
    - `uname`：优先用可改昵称 `TUserDetail.uname`，缺失时回落到脱敏后的注册名；
    - `level`：直接取 `TUserLevel.current_level`；
    - `vip_status`：整型状态转字符串，与历史出参口径一致（前端按 '1' 判断大会员）；
    - 额外补充数据库里已有但此前未返回的字段：`vip_due_date` / `exp`（当前经验） /
      `role`（角色）/ 脱敏后的 `email`。
    """
    uname = (
        detail.uname if (detail and detail.uname) else _mask_user_name(info.user_name)
    )
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
    return CommentUserBrief(
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


class PptrUserService:
    """pptr 用户信息只读访问（批量取 / 昵称搜索）。"""

    @staticmethod
    async def get_many(
        mids: list[int] | set[int],
        *,
        session: AsyncSession | None = None,
    ) -> dict[int, CommentUserBrief]:
        """批量取用户展示信息，返回 `mid -> 用户信息` 字典。

        评论 / 私信列表装配的核心：先收集本页所有 mid（含被回复者、被@者），
        一次 `WHERE uid IN (...)` 取回，**查询次数与列表条数无关**。

        `session` 缺省时内部自建只读会话（服务层内部调用最常用）；也可由调用方
        传入既有 pptr 只读会话以复用连接。
        """
        unique = {int(m) for m in mids if m}
        if not unique:
            return {}

        stmt = _base_select().where(
            col(PptrUserInfo.uid).in_(unique),
            col(PptrUserInfo.deletedAt).is_(None),
        )

        async def _run(s: AsyncSession) -> dict[int, CommentUserBrief]:
            rows = (await s.exec(stmt)).all()
            return {
                int(info.uid): _build_brief(info, detail, vip, level)
                for info, detail, vip, level in rows
            }

        if session is not None:
            return await _run(session)
        async with new_pptr_session() as s:
            return await _run(s)

    @staticmethod
    async def search_by_uname(
        keyword: str,
        limit: int = 10,
        *,
        session: AsyncSession | None = None,
    ) -> list[CommentUserBrief]:
        """按昵称 / 注册名前缀搜索用户（@ 面板用）。

        走前缀匹配 `keyword%`（而非 `%keyword%` 左模糊），对索引友好；
        同时匹配可改昵称 `uname` 与注册名 `user_name`。
        """
        keyword = (keyword or "").strip()
        if not keyword:
            return []
        # 转义 LIKE 通配符，避免用户输入 % / _ 造成异常匹配
        escaped = keyword.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        pattern = f"{escaped}%"

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

        async def _run(s: AsyncSession) -> list[CommentUserBrief]:
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

    @staticmethod
    async def search_users(
        keyword: str,
        offset: int = 0,
        limit: int = 20,
        *,
        session: AsyncSession | None = None,
    ) -> tuple[list[PptrUserSearchItem], bool]:
        """管理端用户搜索（对齐 pptr `GET /api/v1/user/search` 的返回结构）。

        与 `@` 面板的 `search_by_uname` 不同，这里返回**完整**的搜索结构
        （level_info / vip / role_info / email 等），供授权、风控等管理场景使用。

        匹配优先级对齐 pptr：精确 mid 命中优先，否则按昵称 / 注册名 / 邮箱模糊匹配；
        返回结果按 mid 升序，支持 `offset + limit` 分页（而非固定 limit）。

        返回值为 `(items, has_more)`：
        - `items`：当前页命中的用户列表；
        - `has_more`：是否还有下一页（本页返回数达到 `limit` 即视为可能还有更多），
          前端据此决定是否继续下拉加载，无需服务端统计 total。

        仅允许 root 调用——调用方需在路由层做权限校验，本方法不做鉴权。
        """
        keyword = (keyword or "").strip()
        if not keyword:
            return [], False
        offset = max(0, int(offset))
        limit = max(1, min(int(limit), 100))

        # 精确 mid 命中：keyword 为纯数字时优先按 uid 精确匹配
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
                uname = (
                    detail.uname
                    if (detail and detail.uname)
                    else _mask_user_name(info.user_name)
                )
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
                role_info = PptrUserRoleInfo(role_name=role, role_description=role_desc)
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

    # ------------------------------------------------------------------
    # pptr-user RPC 支撑：单用户读取 / 创建（由 be-message 直接接管 pptr Postgres）
    # ------------------------------------------------------------------

    @staticmethod
    async def get_user_profile(
        uid: int | None = None,
        user_name: str | None = None,
        *,
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
        """按 uid 或 user_name 取齐四张表的一行原始记录，不存在返回 None。

        供 RPC `get_user_info` 拼装完整档案使用；登录鉴权常用 user_name 查询。
        """
        if uid:
            cond = col(PptrUserInfo.uid) == int(uid)
        elif user_name:
            cond = col(PptrUserInfo.user_name) == str(user_name)
        else:
            return None

        stmt = _base_select().where(cond, col(PptrUserInfo.deletedAt).is_(None))

        async def _run(s: AsyncSession):
            return (await s.exec(stmt)).first()

        if session is not None:
            return await _run(session)
        async with new_pptr_session() as s:
            return await _run(s)

    @staticmethod
    async def get_user_nav_data(
        uid: int,
        *,
        session: AsyncSession | None = None,
    ) -> PptrUserNavData | None:
        """一次调用拿齐导航栏全部数据（含每日登录加经验、等级计算、邮件脱敏）。

        pptr 通过 RPC `get_user_nav` 直接调用本方法，一次 RPC 即完成加经验 + 数据查询。
        加经验失败仅降级，不阻断 nav 数据返回。
        """

        # 每日首次登录加经验（幂等），失败降级不影响 nav 返回
        await PptrUserService.add_daily_login_exp(uid=uid)

        async def _run(s: AsyncSession) -> PptrUserNavData | None:
            row = (
                await s.exec(_base_select().where(col(PptrUserInfo.uid) == int(uid)))
            ).first()
            if row is None:
                return None
            info, detail, _vip, level = row
            role = (info.role or "level0").lower()
            lv_info = _level_calc(level.current_exp if level else 0, uid=uid)
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

    @staticmethod
    async def create_user(
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
        session: AsyncSession | None = None,
    ) -> tuple[int, bool]:
        """创建用户（一次性写入 TUserInfo + TUserDetail + TUserLevel + TUserVip）。

        供 RPC `create_user` 调用；pptr 不再维护本地 sequelize 用户表。
        uid 为 0 时由 Postgres 自增生成主键，并返回真实 uid。

        Args:
            createdAt: ISO 字符串；为空时由数据库 server_default 填充当前时间。
            birthday: ISO 字符串；为空时不写 TUserDetail.birthday。
            face: B站头像 URL，映射到 TUserDetail.avatar。
            vip_due_date: 整型时间戳（秒），映射到 TUserVip.vip_due_date。

        Returns:
            (uid, created)：uid 为实际主键；created=True 新建 / False 已存在未重复创建。
        """

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

        # uid 为 0 时由雪花算法生成；非 0 时保留原值（外部指定 uid 的场景）
        _uid = uid if uid else generate_uid()

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
            # 先按 user_name 判重（user_name 是业务唯一键）
            exists_uid = await s.scalar(
                func.min(PptrUserInfo.uid)
                .select()
                .select_from(PptrUserInfo)
                .where(PptrUserInfo.user_name == user_name)
            )
            if exists_uid is not None:
                # 用户已存在：upsert 扩展表（TUserDetail / TUserLevel / TUserVip），
                # 但不覆盖 TUserInfo.pwd（Casdoor token 仓库由 update_user_info 管理）。
                real_uid = int(exists_uid)
                await PptrUserService._upsert_profile(s, real_uid, detail, level, vip)
                await s.commit()
                return real_uid, False
            # 昵称唯一校验（对齐 B 站规则）：新用户注册时昵称不可与已有用户重复
            # 三方登录（Casdoor / OAuth）昵称可能与本地用户冲突，此时自动分配 bili_ + uuid
            if uname and await PptrUserService._uname_taken(uname, session=s):
                detail.uname = f"bili_{uuid.uuid4().hex[:12]}"
            # 显式控制 flush 顺序：FK 链为 TUserInfo.uid <- TUserDetail.mid <- (TUserLevel/TUserVip).mid
            # 由于未声明 Relationship，SQLAlchemy UoW 无法自动推断依赖，必须先 flush TUserInfo，
            # 让 uid 在当前事务内可见，再写子表，否则会触发 ForeignKeyViolationError
            s.add(info)
            await s.flush()
            s.add(detail)
            await s.flush()
            s.add(level)
            s.add(vip)
            await s.commit()
            await s.refresh(info)
            return int(info.uid), True

        if session is not None:
            return await _run(session)
        async with new_pptr_session() as s:
            return await _run(s)

    @staticmethod
    async def _upsert_profile(s: AsyncSession, uid: int, detail, level, vip) -> None:
        """按 uid（mid）upsert TUserDetail / TUserLevel / TUserVip（不触碰 TUserInfo.pwd）。"""

        existing = await s.exec(select(PptrUserDetail).where(PptrUserDetail.mid == uid))
        d = existing.first()
        if d is None:
            detail.mid = uid
            s.add(detail)
            # 必须先 flush TUserDetail，否则下游 TUserLevel/TUserVip 的 mid 外键
            # （链式引用 TUserDetail.mid）可能因 UoW 未识别依赖而先被 flush，触发 FK 违约
            await s.flush()
        else:
            if detail.uname is not None:
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

        lv = (
            await s.exec(select(PptrUserLevel).where(PptrUserLevel.mid == uid))
        ).first()
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

    @staticmethod
    async def update_user_info(
        *,
        uid: int = 0,
        user_name: str = "",
        pwd: str | None = None,
        reg_ip_info_id: int = 0,
        session: AsyncSession | None = None,
    ) -> tuple[int, bool]:
        """更新用户（按 uid 或 user_name 更新 TUserInfo.pwd / reg_ip_info_id）。

        供 RPC `update_user_info` 调用（Casdoor token 落库、注册 IP 登记等）。
        返回 (uid, updated)：updated=False 表示用户不存在。
        """
        stmt = select(PptrUserInfo)
        if uid:
            stmt = stmt.where(PptrUserInfo.uid == int(uid))
        elif user_name:
            stmt = stmt.where(PptrUserInfo.user_name == str(user_name))
        else:
            return 0, False

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

    # ------------------------------------------------------------------
    # 等级 / 详情 / 角色 的原子读写（经验算法仍留在 pptr，仅数据落库走 RPC）
    # ------------------------------------------------------------------

    @staticmethod
    async def _uname_taken(
        uname: str, *, self_uid: int | None = None, session: AsyncSession | None = None
    ) -> bool:
        """判断昵称 `uname` 是否已被其他用户占用（对齐 B 站昵称唯一规则）。

        self_uid 传入时排除「本人」，用于改名场景（本人可保留原昵称）。
        昵称为空（None / ""）视为未设置，不占用、不冲突。
        """
        if not uname:
            return False

        async def _run(s: AsyncSession) -> bool:
            stmt = select(PptrUserDetail.mid).where(PptrUserDetail.uname == str(uname))
            result = await s.exec(stmt)
            rows = result.all()
            for mid in rows:
                if self_uid is None or int(mid) != int(self_uid):
                    return True
            return False

        if session is not None:
            return await _run(session)
        async with new_pptr_session() as s:
            return await _run(s)

    @staticmethod
    async def get_user_level(uid: int) -> tuple[int, int, int, str] | None:
        """按 uid 取 TUserLevel (current_level, current_exp, current_min, updated_at_iso)。"""

        async with new_pptr_session() as s:
            result = await s.exec(
                select(PptrUserLevel).where(PptrUserLevel.mid == int(uid))
            )
            lv = result.one_or_none()
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

    @staticmethod
    async def set_user_level(
        *, uid: int, current_level: int, current_exp: int, current_min: int
    ) -> bool:
        """原子写入 TUserLevel（经验算法在 pptr 侧算好后调用）。"""

        async with new_pptr_session() as s:
            lv = (
                await s.exec(select(PptrUserLevel).where(PptrUserLevel.mid == int(uid)))
            ).first()
            if lv is None:
                s.add(
                    PptrUserLevel(
                        mid=int(uid),
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

    @staticmethod
    async def set_user_detail(
        *,
        uid: int,
        uname: str = "",
        face: str | None = None,
        sign: str = "",
        sex: str = "保密",
        email: str = "",
        birthday: str = "",
    ) -> bool:
        """更新 TUserDetail（face 映射到 avatar；birthday 为 ISO 字符串）。"""
        import datetime as datetime

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
                    select(PptrUserDetail).where(PptrUserDetail.mid == int(uid))
                )
            ).first()
            if d is None:
                # 昵称唯一校验（新建行）：不可与已有用户重复
                if uname and await PptrUserService._uname_taken(
                    uname, self_uid=int(uid), session=s
                ):
                    raise ResourceConflictException("昵称已被占用，请更换昵称后重试")
                s.add(
                    PptrUserDetail(
                        mid=int(uid),
                        uname=uname or None,
                        avatar=face or None,
                        sign=sign or None,
                        sex=sex or None,
                        email=email or None,
                        birthday=parsed_birthday,
                    )
                )
            else:
                # 昵称唯一校验（改名）：仅在昵称实际变更且与他人冲突时拦截
                if uname and uname != (d.uname or ""):
                    if await PptrUserService._uname_taken(
                        uname, self_uid=int(uid), session=s
                    ):
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

    @staticmethod
    async def set_user_role(*, uid: int, role: str) -> bool:
        """更新 TUserInfo.role（root 角色不可被非 root 覆盖，由 be-message 侧保护）。"""
        # 合法角色统一取自 bili_common（level0..level6 + root），
        # 避免此处硬编码漏掉 level5 / level6 导致与网关校验口径不一致。
        if role not in VALID_ROLES:
            return False
        async with new_pptr_session() as s:
            info = (
                await s.exec(select(PptrUserInfo).where(PptrUserInfo.uid == int(uid)))
            ).first()
            if not info:
                return False
            # root 是独立管理员角色，仅允许显式设置为 root，不可被覆盖为其它
            if info.role == "root" and role != "root":
                return False
            info.role = role
            s.add(info)
            await s.commit()
            return True

    @staticmethod
    def _level_to_role(level: int) -> str:
        """由纯数字等级（0~max）生成成长等级角色（对齐 pptr levelToRole）。"""
        lv = int(level)
        if 0 <= lv <= settings.level_max_level:
            return f"level{lv}"
        return "level0"

    @staticmethod
    async def sync_role_on_level_up(*, uid: int, new_level: int) -> bool:
        """升级时自动同步成长等级角色（业务逻辑下沉 be-message）。

        对齐 pptr `syncRoleOnLevelUp`：等级上升且当前角色非 root 时，
        将 TUserInfo.role 更新为对应的 level{n}；root 角色不被升级逻辑覆盖。
        返回是否发生了角色更新。
        """
        async with new_pptr_session() as s:
            info = (
                await s.exec(select(PptrUserInfo).where(PptrUserInfo.uid == int(uid)))
            ).first()
            if not info:
                return False
            if info.role == "root":
                return False
            target_role = PptrUserService._level_to_role(new_level)
            if info.role == target_role:
                return False
            info.role = target_role
            s.add(info)
            await s.commit()
            return True

    @staticmethod
    async def add_exp(*, uid: int, exp: int, action_type: str = "") -> dict:
        """增加经验值（业务逻辑整体在 be-message 完成，pptr 仅传 uid/exp）。

        读取当前等级 -> 计算新经验 / 新等级 -> 原子落库 -> 升级时同步角色（不覆盖 root）。
        当 action_type 不为空时，在 TUserExpRecord 中记录本次经验增加来源。
        返回与 pptr `UserLevelService.add_exp` 完全对齐的结果字典。
        """
        level_row = await PptrUserService.get_user_level(uid)
        if level_row is None:
            old_level = 0
            old_exp = 0
        else:
            old_level, old_exp, _, _ = level_row

        new_exp = int(old_exp) + int(exp)
        calc = _level_calc(new_exp, uid=uid)
        new_level = calc.current_level

        await PptrUserService.set_user_level(
            uid=uid,
            current_level=calc.current_level,
            current_exp=calc.current_exp,
            current_min=calc.current_min,
        )

        if action_type:
            today_str = datetime.datetime.now().astimezone().strftime("%Y-%m-%d")
            # 如果是字符串枚举名，转为 int；否则直接作为 int 使用
            try:
                at = ExpActionType[action_type.upper()].value
            except (KeyError, AttributeError):
                at = int(action_type)
            async with new_pptr_session() as s:
                s.add(
                    PptrUserExpRecord(
                        mid=int(uid),
                        action_type=at,
                        exp=int(exp),
                        ref_date=today_str,
                    )
                )
                await s.commit()

        role_updated = False
        if new_level > old_level:
            role_updated = await PptrUserService.sync_role_on_level_up(
                uid=uid, new_level=new_level
            )

        return {
            "uid": int(uid),
            "old_exp": int(old_exp),
            "new_exp": int(new_exp),
            "old_level": int(old_level),
            "new_level": int(new_level),
            "leveled_up": new_level > old_level,
            "role_updated": role_updated,
        }

    @staticmethod
    async def add_daily_login_exp(*, uid: int) -> dict:
        """每日首次登录加经验（业务逻辑整体在 be-message 完成）。

        基于 TUserExpRecord 表做每日幂等检查（action_type='daily_login' + 当天日期）；
        今日已领则直接返回 can_add_exp=False；否则加经验、计算等级、升级同步角色，
        并在 TUserExpRecord 中记录本次增加。
        返回含完整 level_info 的结果字典。
        """
        today_str = datetime.datetime.now().astimezone().strftime("%Y-%m-%d")

        # 查 TUserExpRecord 表：今日是否有 daily_login 记录
        async with new_pptr_session() as s:
            existing = (
                await s.exec(
                    select(PptrUserExpRecord).where(
                        PptrUserExpRecord.mid == int(uid),
                        PptrUserExpRecord.action_type == ExpActionType.DAILY_LOGIN.value,
                        PptrUserExpRecord.ref_date == today_str,
                    )
                )
            ).first()

        if existing is not None:
            # 今天已经领取过
            level_row = await PptrUserService.get_user_level(uid)
            if level_row is None:
                old_level, old_exp = 0, 0
            else:
                old_level, old_exp, _, _ = level_row
            calc = _level_calc(old_exp, uid=uid)
            return {
                "uid": int(uid),
                "can_add_exp": False,
                "old_exp": int(old_exp),
                "new_exp": int(old_exp),
                "old_level": int(old_level),
                "new_level": int(old_level),
                "leveled_up": False,
                "role_updated": False,
                "level_info": calc,
            }

        level_row = await PptrUserService.get_user_level(uid)
        if level_row is None:
            old_level = 0
            old_exp = 0
        else:
            old_level, old_exp, _, _ = level_row

        daily_exp = settings.level_daily_exp_bonus
        new_exp = int(old_exp) + int(daily_exp)
        calc = _level_calc(new_exp, uid=uid)
        new_level = calc.current_level

        await PptrUserService.set_user_level(
            uid=uid,
            current_level=calc.current_level,
            current_exp=calc.current_exp,
            current_min=calc.current_min,
        )

        # 记录本次经验增加
        async with new_pptr_session() as s:
            s.add(
                PptrUserExpRecord(
                    mid=int(uid),
                    action_type=ExpActionType.DAILY_LOGIN.value,
                    exp=int(daily_exp),
                    ref_date=today_str,
                )
            )
            await s.commit()

        role_updated = False
        if new_level > old_level:
            role_updated = await PptrUserService.sync_role_on_level_up(
                uid=uid, new_level=new_level
            )

        return {
            "uid": int(uid),
            "can_add_exp": True,
            "old_exp": int(old_exp),
            "new_exp": int(new_exp),
            "old_level": int(old_level),
            "new_level": int(new_level),
            "leveled_up": new_level > old_level,
            "role_updated": role_updated,
            "level_info": calc,
        }

    @staticmethod
    async def add_username_record(*, uid: int, prev_uname: str | None) -> bool:
        """记录昵称变更历史到 TUserNameRecord。"""
        async with new_pptr_session() as s:
            s.add(
                PptrUserNameRecord(
                    mid=uid,
                    prev_uname=prev_uname,
                )
            )
            await s.commit()
        return True

__all__ = ["PptrUserService"]
