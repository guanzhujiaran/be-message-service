"""系统通知服务。

存储模型采用**读扩散**：通知本体全站只存一份（`msg_notify`），
用户侧只维护「游标」（`msg_notify_cursor`）与「已读状态」（`msg_notify_state`）。

为什么不写扩散：系统通知一条可能面向全站用户，写扩散会瞬间产生
N 条冗余行，对小设备是灾难；而通知的读取频率远低于私信，
读时用一次 LEFT JOIN 过滤已读完全够用。

**避免重复消费**由三层机制共同保证：

1. 拉取游标 `last_notify_id`：每次只捞 `id > cursor` 的增量，拉完推进游标。
2. 已读状态表 `(mid, notify_id)` 唯一索引：并发标记已读天然幂等。
3. 通知本体的 `dispatched` 标记：定时推送任务只处理未投递过的通知，
   投递成功后立刻置位，任务重入不会重复推送。
"""

from datetime import datetime

from loguru import logger
from sqlalchemy import Integer, and_, cast, func, or_
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from bili_common.models.depends import AuthInfo

from app.core.database import new_session
from app.models.db import NotifyCursor, NotifyMessage, NotifyState, UserMessageSetting
from app.models.enums import (
    NotifyLevelEnum,
    NotifyStatusEnum,
    NotifyTargetTypeEnum,
)
from app.models.schemas import (
    NotifyAdminItem,
    NotifyCreateReq,
    NotifyItem,
    NotifyPullResp,
    NotifyReadResp,
    NotifyUpdateReq,
)


def _is_vip(user: AuthInfo) -> bool:
    """大会员判定：vip_status 非空且不为 0。"""
    status = (user.vip_status or "").strip()
    return bool(status) and status not in {"0", "false", "False"}


def _target_condition(user: AuthInfo):
    """构造「该通知是否投放给当前用户」的 SQL 条件。

    按用户类型推送的核心：把受众规则表达成 WHERE 条件，
    在数据库侧一次性过滤，无需把全部通知拉到内存再筛。
    """
    table = NotifyMessage
    conditions = [
        # 全体用户
        table.target_type == NotifyTargetTypeEnum.ALL,
        # 按角色精确匹配
        and_(
            table.target_type == NotifyTargetTypeEnum.ROLE,
            table.target_value == user.role,
        ),
        # 按等级：用户等级 >= 通知要求的最低等级
        and_(
            table.target_type == NotifyTargetTypeEnum.LEVEL,
            cast(table.target_value, Integer) <= user.level,
        ),
        # 指定 mid 列表（逗号分隔）
        and_(
            table.target_type == NotifyTargetTypeEnum.CUSTOM,
            func.find_in_set(
                str(user.mid), func.replace(table.target_value, " ", "")
            )
            > 0,
        ),
    ]
    if _is_vip(user):
        conditions.append(table.target_type == NotifyTargetTypeEnum.VIP)
    return or_(*conditions)


def _visible_condition(now: datetime | None = None):
    """构造「通知当前对用户可见」的 SQL 条件：已发布、已到生效时间、未过期。

    `now` 默认使用数据库自身时钟 `func.now()`。**不要**在此嵌入 Python 侧
    `datetime.now()` 字面量：SQLAlchemy 会把该字面量编译进语句缓存的绑定参数，
    在多次执行时偶发复用旧的 `now` 值（实测表现为刚发布的通知因
    `publish_at <= now` 命中陈旧时间而被判为「未到生效」、拉取时忽隐忽现）。
    与 `create` 写入 `publish_at` 时同样使用 `func.now()`，两端同源、无偏移。
    """
    from sqlalchemy import func as _func

    now_expr = now if now is not None else _func.now()
    return and_(
        NotifyMessage.status == NotifyStatusEnum.PUBLISHED,
        NotifyMessage.publish_at <= now_expr,
        or_(NotifyMessage.expire_at.is_(None), NotifyMessage.expire_at > now_expr),  # type: ignore[union-attr]
    )


class NotifyService:
    """系统通知的发布、拉取与已读管理。"""

    # ==================== 管理员侧 ====================

    @staticmethod
    async def create(
        session: AsyncSession, creator_mid: int, req: NotifyCreateReq
    ) -> NotifyAdminItem:
        """管理员发布通知。

        `publish_now=False` 时存为草稿；`publish_at` 为未来时间时即为定时发布，
        到点后由定时任务（NotifyService.dispatch_pending）自动投递推送。
        """
        row = NotifyMessage(
            title=req.title,
            content=req.content,
            jump_url=req.jump_url,
            target_type=req.target_type,
            target_value=req.target_value,
            level=req.level,
            status=(
                NotifyStatusEnum.PUBLISHED if req.publish_now else NotifyStatusEnum.DRAFT
            ),
            # 用数据库时钟，与 _visible_condition 的 `publish_at <= now()` 同源，
            # 避免服务侧 datetime.now() 与 DB now() 的亚秒偏移导致刚发布即被判不可见。
            publish_at=req.publish_at or func.now(),
            expire_at=req.expire_at,
            creator_mid=creator_mid,
        )
        session.add(row)
        await session.commit()
        await session.refresh(row)
        logger.info(
            f"管理员 {creator_mid} 发布通知 id={row.id} "
            f"target={row.target_type}:{row.target_value} status={row.status}"
        )
        return NotifyService._to_admin_item(row)

    @staticmethod
    async def send_to_user(
        mid: int,
        title: str,
        content: str,
        level: NotifyLevelEnum = NotifyLevelEnum.NORMAL,
        jump_url: str | None = None,
        creator_mid: int = 0,
    ) -> None:
        """向单个用户发送系统通知（CUSTOM 定向投放）。

        使用独立事务写入，与调用方主事务解耦：通知失败不影响主流程（弱依赖）。
        用于「评论 / 私信状态变更」等需要主动告知相关用户的场景。
        """
        try:
            async with new_session() as s:
                s.add(
                    NotifyMessage(
                        title=title,
                        content=content,
                        jump_url=jump_url,
                        target_type=NotifyTargetTypeEnum.CUSTOM,
                        target_value=str(mid),
                        level=level,
                        status=NotifyStatusEnum.PUBLISHED,
                        publish_at=func.now(),
                        creator_mid=creator_mid,
                    )
                )
                await s.commit()
        except Exception as e:  # noqa: BLE001
            logger.warning(f"向用户 {mid} 发送系统通知失败（弱依赖，已忽略）: {e}")

    @staticmethod
    async def update(
        session: AsyncSession, notify_id: int, req: NotifyUpdateReq
    ) -> NotifyAdminItem | None:
        row = await session.get(NotifyMessage, notify_id)
        if row is None:
            return None
        changes = req.model_dump(exclude_unset=True, exclude_none=True)
        for field, value in changes.items():
            setattr(row, field, value)
        row.updated_at = datetime.now()
        session.add(row)
        await session.commit()
        await session.refresh(row)
        return NotifyService._to_admin_item(row)

    @staticmethod
    async def revoke(session: AsyncSession, notify_id: int) -> bool:
        """撤回通知：用户侧立即不可见。"""
        row = await session.get(NotifyMessage, notify_id)
        if row is None:
            return False
        row.status = NotifyStatusEnum.REVOKED
        row.updated_at = datetime.now()
        session.add(row)
        await session.commit()
        logger.info(f"通知 {notify_id} 已撤回")
        return True

    @staticmethod
    async def admin_list(
        session: AsyncSession,
        page_num: int = 1,
        page_size: int = 20,
        status: NotifyStatusEnum | None = None,
    ) -> tuple[list[NotifyAdminItem], int]:
        stmt = select(NotifyMessage)
        count_stmt = select(func.count()).select_from(NotifyMessage)
        if status is not None:
            stmt = stmt.where(NotifyMessage.status == status)
            count_stmt = count_stmt.where(NotifyMessage.status == status)
        total = int((await session.exec(count_stmt)).one() or 0)
        stmt = (
            stmt.order_by(NotifyMessage.id.desc())  # type: ignore[union-attr]
            .offset((page_num - 1) * page_size)
            .limit(page_size)
        )
        rows = (await session.exec(stmt)).all()
        return [NotifyService._to_admin_item(r) for r in rows], total

    # ==================== 用户侧 ====================

    @staticmethod
    async def pull(
        session: AsyncSession,
        user: AuthInfo,
        cursor: int | None = None,
        limit: int = 20,
    ) -> NotifyPullResp:
        """定时拉取增量通知（避免重复消费的主入口）。

        游标语义：只返回 `id > cursor` 的通知。cursor 未显式传入时使用
        服务端为该用户持久化的 `last_notify_id`，因此即使客户端丢失本地状态，
        也不会把老通知重新拉一遍。
        """
        cursor_row = await NotifyService._get_or_create_cursor(session, user.mid)
        effective_cursor = cursor if cursor is not None else cursor_row.last_notify_id

        stmt = (
            select(NotifyMessage)
            .where(
                _visible_condition(),
                _target_condition(user),
                NotifyMessage.id > effective_cursor,  # type: ignore[operator]
            )
            .order_by(NotifyMessage.id.asc())  # type: ignore[union-attr]
            .limit(limit + 1)
        )
        rows = list((await session.exec(stmt)).all())
        has_more = len(rows) > limit
        rows = rows[:limit]

        # 批量取这批通知的已读状态，避免逐条查询
        read_map = await NotifyService._read_state_map(
            session, user.mid, [r.id for r in rows if r.id is not None]
        )

        items = [
            NotifyItem(
                id=r.id or 0,
                title=r.title,
                content=r.content,
                jump_url=r.jump_url,
                level=r.level,
                publish_at=r.publish_at,
                expire_at=r.expire_at,
                is_read=read_map.get(r.id or 0, (False, None))[0],
                read_at=read_map.get(r.id or 0, (False, None))[1],
                dispatched=bool(r.dispatched),
            )
            for r in rows
        ]

        # 推进服务端游标（只增不减，避免并发拉取导致游标回退）
        new_cursor = max([effective_cursor, *[i.id for i in items]]) if items else effective_cursor
        if new_cursor > cursor_row.last_notify_id:
            cursor_row.last_notify_id = new_cursor
        cursor_row.last_pull_at = datetime.now()
        session.add(cursor_row)
        await session.commit()

        return NotifyPullResp(
            items=items,
            cursor=new_cursor,
            unread_count=await NotifyService.unread_count(session, user),
            has_more=has_more,
        )

    @staticmethod
    async def list_for_user(
        session: AsyncSession,
        user: AuthInfo,
        page_num: int = 1,
        page_size: int = 20,
        only_unread: bool = False,
    ) -> tuple[list[NotifyItem], int]:
        """分页查看历史通知（与 pull 不同，不推进游标）。"""
        state = NotifyState
        join_cond = and_(state.notify_id == NotifyMessage.id, state.mid == user.mid)

        base_conditions = [
            _visible_condition(),
            _target_condition(user),
            or_(state.is_deleted.is_(None), state.is_deleted == False),  # noqa: E712
        ]
        if only_unread:
            base_conditions.append(
                or_(state.is_read.is_(None), state.is_read == False)  # noqa: E712
            )

        count_stmt = (
            select(func.count())
            .select_from(NotifyMessage)
            .outerjoin(state, join_cond)
            .where(*base_conditions)
        )
        total = int((await session.exec(count_stmt)).one() or 0)

        stmt = (
            select(NotifyMessage, state.is_read, state.read_at)
            .outerjoin(state, join_cond)
            .where(*base_conditions)
            .order_by(NotifyMessage.id.desc())  # type: ignore[union-attr]
            .offset((page_num - 1) * page_size)
            .limit(page_size)
        )
        rows = (await session.exec(stmt)).all()
        items = [
            NotifyItem(
                id=n.id or 0,
                title=n.title,
                content=n.content,
                jump_url=n.jump_url,
                level=n.level,
                publish_at=n.publish_at,
                expire_at=n.expire_at,
                is_read=bool(is_read),
                read_at=read_at,
                dispatched=bool(n.dispatched),
            )
            for n, is_read, read_at in rows
        ]
        return items, total

    @staticmethod
    async def unread_count(session: AsyncSession, user: AuthInfo) -> int:
        """未读数 = 可见通知数 - 已读/已删除数（一次 LEFT JOIN 算出）。"""
        state = NotifyState
        stmt = (
            select(func.count())
            .select_from(NotifyMessage)
            .outerjoin(
                state,
                and_(state.notify_id == NotifyMessage.id, state.mid == user.mid),
            )
            .where(
                _visible_condition(),
                _target_condition(user),
                or_(state.id.is_(None), and_(state.is_read == False, state.is_deleted == False)),  # noqa: E712
            )
        )
        return int((await session.exec(stmt)).one() or 0)

    @staticmethod
    async def mark_read(
        session: AsyncSession, user: AuthInfo, notify_ids: list[int] | None = None
    ) -> NotifyReadResp:
        """标记已读。

        `notify_ids` 为空时标记全部可见通知为已读。
        写入走 `INSERT ... ON DUPLICATE KEY UPDATE`，配合 (mid, notify_id) 唯一索引，
        重复调用不会产生脏数据。
        """
        if notify_ids is None:
            stmt = select(NotifyMessage.id).where(
                _visible_condition(), _target_condition(user)
            )
            notify_ids = [i for i in (await session.exec(stmt)).all() if i is not None]

        if not notify_ids:
            return NotifyReadResp(
                affected=0, unread_count=await NotifyService.unread_count(session, user)
            )

        now = datetime.now()
        insert_stmt = mysql_insert(NotifyState.__table__).values(
            [
                {
                    "mid": user.mid,
                    "notify_id": nid,
                    "is_read": True,
                    "read_at": now,
                    "is_deleted": False,
                    "created_at": now,
                    "updated_at": now,
                }
                for nid in set(notify_ids)
            ]
        )
        insert_stmt = insert_stmt.on_duplicate_key_update(
            is_read=True, read_at=now, updated_at=now
        )
        await session.exec(insert_stmt)  # type: ignore[call-overload]
        await session.commit()

        return NotifyReadResp(
            affected=len(set(notify_ids)),
            unread_count=await NotifyService.unread_count(session, user),
        )

    @staticmethod
    async def delete_for_user(
        session: AsyncSession, user: AuthInfo, notify_ids: list[int]
    ) -> int:
        """用户删除通知（仅自己不可见，不影响其他用户）。"""
        if not notify_ids:
            return 0
        now = datetime.now()
        insert_stmt = mysql_insert(NotifyState.__table__).values(
            [
                {
                    "mid": user.mid,
                    "notify_id": nid,
                    "is_read": True,
                    "read_at": now,
                    "is_deleted": True,
                    "created_at": now,
                    "updated_at": now,
                }
                for nid in set(notify_ids)
            ]
        )
        insert_stmt = insert_stmt.on_duplicate_key_update(
            is_deleted=True, updated_at=now
        )
        await session.exec(insert_stmt)  # type: ignore[call-overload]
        await session.commit()
        return len(set(notify_ids))

    # ==================== 定时投递 ====================

    @staticmethod
    async def fetch_dispatchable(
        session: AsyncSession, limit: int = 20
    ) -> list[NotifyMessage]:
        """取出「已到发布时间但尚未投递推送」的通知。

        `dispatched` 标记是避免定时任务重复推送的关键：
        任务每分钟跑一次，只会捞到还没投递过的那些。
        """
        stmt = (
            select(NotifyMessage)
            .where(_visible_condition(), NotifyMessage.dispatched == False)  # noqa: E712
            .order_by(NotifyMessage.id.asc())  # type: ignore[union-attr]
            .limit(limit)
        )
        return list((await session.exec(stmt)).all())

    @staticmethod
    async def mark_dispatched(session: AsyncSession, notify_id: int) -> None:
        row = await session.get(NotifyMessage, notify_id)
        if row is None:
            return
        row.dispatched = True
        row.dispatched_at = datetime.now()
        session.add(row)
        await session.commit()

    @staticmethod
    async def resolve_target_mids(
        session: AsyncSession, notify: NotifyMessage, limit: int = 2000
    ) -> list[int]:
        """解析一条通知的推送目标 mid 列表。

        本服务不持有用户档案表（用户信息来自网关透传的 x-bili-* 头），
        因此除 `custom` 外的投放类型无法在后台离线求解精确受众。
        折中策略：**推送投递面向本服务见过的用户**（有消息设置记录的 mid），
        而「这条通知到底对谁可见」仍由用户拉取时的 `_target_condition` 精确判定，
        不会出现「推送到了但列表里看不到」之外的正确性问题。
        """
        if notify.target_type is NotifyTargetTypeEnum.CUSTOM:
            raw = (notify.target_value or "").replace(" ", "")
            mids: list[int] = []
            for part in raw.split(","):
                if part.isdigit():
                    mids.append(int(part))
            return mids[:limit]

        stmt = select(UserMessageSetting.mid).where(
            UserMessageSetting.recv_notify == True  # noqa: E712
        ).limit(limit)
        return [int(m) for m in (await session.exec(stmt)).all()]

    # ==================== 内部方法 ====================

    @staticmethod
    async def _get_or_create_cursor(session: AsyncSession, mid: int) -> NotifyCursor:
        stmt = select(NotifyCursor).where(NotifyCursor.mid == mid)
        row = (await session.exec(stmt)).one_or_none()
        if row is not None:
            return row
        row = NotifyCursor(mid=mid, last_notify_id=0)
        session.add(row)
        try:
            await session.commit()
            await session.refresh(row)
        except Exception:
            await session.rollback()
            row = (await session.exec(stmt)).one_or_none()
            if row is None:
                raise
        return row

    @staticmethod
    async def _read_state_map(
        session: AsyncSession, mid: int, notify_ids: list[int]
    ) -> dict[int, tuple[bool, datetime | None]]:
        if not notify_ids:
            return {}
        stmt = select(
            NotifyState.notify_id, NotifyState.is_read, NotifyState.read_at
        ).where(NotifyState.mid == mid, NotifyState.notify_id.in_(notify_ids))  # type: ignore[union-attr]
        return {nid: (bool(is_read), read_at) for nid, is_read, read_at in (await session.exec(stmt)).all()}

    @staticmethod
    def _to_admin_item(row: NotifyMessage) -> NotifyAdminItem:
        return NotifyAdminItem(
            id=row.id or 0,
            title=row.title,
            content=row.content,
            jump_url=row.jump_url,
            target_type=row.target_type,
            target_value=row.target_value,
            level=row.level,
            status=row.status,
            publish_at=row.publish_at,
            expire_at=row.expire_at,
            creator_mid=row.creator_mid,
            dispatched=row.dispatched,
            created_at=row.created_at,
        )


__all__ = ["NotifyService"]
