"""评论读服务：列表、详情、计数。

对应 B 站评论架构中 `reply-interface` 的「数据组装」职责。

**本模块最重要的性能约束：单次列表请求的 SQL 次数必须是常量，
与返回的评论条数无关。** 实现手段是「先批量取 ID，再按 ID 集合批量回捞」：

    1 次 主列表（索引表）
    1 次 正文（WHERE rpid IN (...)）
    1 次 用户快照（WHERE mid IN (...)）
    1 次 当前用户互动态（WHERE rpid IN (...) AND mid = ?）
    ─────────────────────────────────
    共 4 次，无论一页是 20 条还是 50 条

任何在循环体里发起查询的写法都会退化成 N+1，是本模块的红线。

另一条红线：**总数一律读评论区冗余计数，禁止 `COUNT(*)`**。
评论区上万条时 `COUNT(*)` 会扫掉整段索引，是最典型的慢查询来源。
"""

from sqlmodel import col, select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.db import CommentAction, CommentContent, CommentIndex, CommentSubject
from app.models.enums import (
    CommentActionEnum,
    CommentAttrBit,
    CommentSortEnum,
    CommentStateEnum,
    CommentSubjectStateEnum,
    CommentTypeEnum,
)
from app.core.config import settings

from app.models.schemas import (
    CommentCountResp,
    CommentItem,
    CommentListResp,
    CommentSubListResp,
    CommentUserBrief,
)
from app.services.comment import VISIBLE_STATES, CommentService
from app.services.pptr_user import PptrUserService
from app.utils.ip_mask import mask_ip_pair


class CommentReadService:
    """评论的读取与视图组装。"""

    # ==================== 列表 ====================

    @staticmethod
    async def list_main(
        session: AsyncSession,
        oid: int,
        type_: CommentTypeEnum,
        *,
        sort: CommentSortEnum = CommentSortEnum.HOT,
        page_num: int = 1,
        page_size: int = 20,
        viewer_mid: int | None = None,
        focus_rpid: int | None = None,
    ) -> CommentListResp:
        """一级评论列表。

        置顶评论**单独查询并从主列表中排除**：如果让它参与排序再在内存里挪到顶部，
        会造成分页错位（第 2 页少一条 / 重复一条）。

        `focus_rpid` 用于「从通知 / 外链直达某条评论」场景：把它（或其根评论）提到
        列表最前面并回填 `focus_rpid` / `focus_root`，前端据此展开楼中楼并滚动定位。
        若该评论不在当前页或不存在，则忽略 focus，不影响正常列表。
        """
        subject = await CommentService.get_subject(session, oid, type_)
        if subject is None:
            # 评论区尚未开区（还没有人评论过），返回空列表而不是 404
            return CommentListResp(
                items=[],
                top=None,
                total=0,
                all_count=0,
                page_num=page_num,
                page_size=page_size,
                subject_state=CommentSubjectStateEnum.NORMAL,
            )

        top_rpid = subject.top_rpid or 0

        # ---- focus 解析：定位需要置顶展示的目标评论 ----
        focus_root = 0
        if focus_rpid:
            focus_row = (
                await session.exec(
                    select(CommentIndex).where(
                        col(CommentIndex.rpid) == focus_rpid,
                        col(CommentIndex.state).in_(VISIBLE_STATES),
                    )
                )
            ).one_or_none()
            if focus_row is not None and focus_row.oid == oid and focus_row.type == type_:
                # 一级评论：自己就是根；楼中楼：取其根评论
                focus_root = focus_row.root if focus_row.root != 0 else focus_row.rpid

        stmt = select(CommentIndex).where(
            col(CommentIndex.oid) == oid,
            col(CommentIndex.type) == type_,
            col(CommentIndex.root) == 0,
            col(CommentIndex.state).in_(VISIBLE_STATES),
        )
        if top_rpid:
            stmt = stmt.where(col(CommentIndex.rpid) != top_rpid)
        if focus_root and focus_root not in (top_rpid, 0):
            # focus 根评论也单独取出，避免它落在当前页之外
            stmt = stmt.where(col(CommentIndex.rpid) != focus_root)

        if sort is CommentSortEnum.HOT:
            # 排序段直接吃 idx_comment_hot 的冗余列，禁止写成表达式
            stmt = stmt.order_by(
                col(CommentIndex.hot_score).desc(), col(CommentIndex.rpid).desc()
            )
        else:
            # rpid 单调递增，倒序即最新优先
            stmt = stmt.order_by(col(CommentIndex.rpid).desc())

        offset = (page_num - 1) * page_size
        rows = (await session.exec(stmt.offset(offset).limit(page_size))).all()

        top_row = None
        if top_rpid and page_num == 1:
            top_row = (
                await session.exec(
                    select(CommentIndex).where(
                        col(CommentIndex.rpid) == top_rpid,
                        col(CommentIndex.state).in_(VISIBLE_STATES),
                    )
                )
            ).one_or_none()

        focus_row_loaded = None
        if focus_root and focus_root not in (top_rpid, 0):
            focus_root_row = (
                await session.exec(
                    select(CommentIndex).where(
                        col(CommentIndex.rpid) == focus_root,
                        col(CommentIndex.state).in_(VISIBLE_STATES),
                    )
                )
            ).one_or_none()
            if focus_root_row is not None:
                # 一次性装配根评论及其全部楼中楼（不止预览条数），前端需完整展开以定位子评论
                subs = (
                    await session.exec(
                        select(CommentIndex).where(
                            col(CommentIndex.root) == focus_root,
                            col(CommentIndex.state).in_(VISIBLE_STATES),
                        ).order_by(col(CommentIndex.rpid))
                    )
                ).all()
                focus_assembled = await CommentReadService.assemble_items(
                    session, [focus_root_row, *subs], viewer_mid=viewer_mid
                )
                focus_row_loaded = focus_assembled[0]
                focus_row_loaded.replies = focus_assembled[1:]

        # 置顶行 / focus 根评论与主列表一起装配，避免多跑一轮批量查询
        extra: list[CommentIndex] = []
        if top_row is not None:
            extra.append(top_row)
        if focus_row_loaded is not None:
            extra.append(focus_row_loaded)  # type: ignore[arg-type]
        assembled = await CommentReadService.assemble_items(
            session, [*extra, *rows], viewer_mid=viewer_mid
        )
        # 楼中楼预览：批量回捞后按 root 分组截断，SQL 次数与一级评论条数无关
        # （focus 根评论已在上面单独装配了完整楼中楼，这里会再挂载一次预览，幂等无害）
        await CommentReadService._attach_sub_previews(
            session, assembled, viewer_mid=viewer_mid
        )
        top_item = assembled[0] if top_row is not None else None
        items = assembled[1:] if top_row is not None else assembled
        # focus 根评论紧随置顶之后，需从主列表里剔除再插到最前（置顶之后）
        if focus_row_loaded is not None:
            items = [it for it in items if it.rpid != str(focus_root)]
            items.insert(0 if top_item is None else 1, focus_row_loaded)  # type: ignore[arg-type]

        return CommentListResp(
            items=items,
            top=top_item,
            total=subject.root_count,
            all_count=subject.all_count,
            page_num=page_num,
            page_size=page_size,
            subject_state=subject.state,
            focus_rpid=str(focus_rpid) if focus_root else None,
            focus_root=str(focus_root) if focus_root else None,
        )

    # ==================== 详情 ====================

    @staticmethod
    async def get_detail(
        session: AsyncSession, rpid: int, *, viewer_mid: int | None = None
    ) -> CommentItem | None:
        """单条评论详情。已删除 / 已下架的评论返回 None。"""
        row = (
            await session.exec(
                select(CommentIndex).where(col(CommentIndex.rpid) == rpid)
            )
        ).one_or_none()
        if row is None or row.state not in VISIBLE_STATES:
            return None
        items = await CommentReadService.assemble_items(
            session, [row], viewer_mid=viewer_mid
        )
        return items[0] if items else None

    # ==================== 计数 ====================

    @staticmethod
    async def get_count(
        session: AsyncSession, oid: int, type_: CommentTypeEnum
    ) -> CommentCountResp:
        """评论区计数（读冗余列，不做 COUNT 扫描）。"""
        subject = await CommentService.get_subject(session, oid, type_)
        if subject is None:
            return CommentCountResp(oid=str(oid), type=type_)
        return CommentCountResp(
            oid=str(subject.oid),
            type=subject.type,
            root_count=subject.root_count,
            all_count=subject.all_count,
            state=subject.state,
        )

    # ==================== 楼中楼（Phase 2.12 / 2.13）====================

    @staticmethod
    async def _attach_sub_previews(
        session: AsyncSession,
        items: list[CommentItem],
        *,
        viewer_mid: int | None = None,
    ) -> None:
        """给一级评论批量挂载楼中楼预览（最多 N 条）。

        关键约束同主列表：**SQL 次数与一级评论条数无关**。
        先一次性 `WHERE root IN (...)` 捞回本页所有根评论的子评论，按 root 分组后
        在内存里截断到 `comment_sub_preview_count`，再走同一套批量装配（正文 / 快照 /
        互动态各一次），不会出现「每条一级评论各查一轮」的 N+1。
        """
        if not items:
            return
        root_ids = [int(it.rpid) for it in items]
        sub_rows = (
            await session.exec(
                select(CommentIndex).where(
                    col(CommentIndex.root).in_(root_ids),
                    col(CommentIndex.state).in_(VISIBLE_STATES),
                )
            )
        ).all()
        by_root: dict[int, list[CommentIndex]] = {}
        for r in sub_rows:
            by_root.setdefault(r.root, []).append(r)

        preview_count = settings.comment_sub_preview_count
        for it in items:
            subs = by_root.get(int(it.rpid), [])[:preview_count]
            if not subs:
                continue
            it.replies = await CommentReadService.assemble_items(
                session, subs, viewer_mid=viewer_mid
            )

    @staticmethod
    async def get_sub_list(
        session: AsyncSession,
        root: int,
        oid: int,
        type_: CommentTypeEnum,
        *,
        page_num: int = 1,
        page_size: int = 20,
        viewer_mid: int | None = None,
    ) -> CommentSubListResp:
        """楼中楼展开分页（收起 / 加载更多）。

        `total` 直接读根评论的冗余 `rcount`，不 `COUNT(*)`；排序按 rpid（同发布顺序）。
        """
        root_row = (
            await session.exec(
                select(CommentIndex).where(col(CommentIndex.rpid) == root)
            )
        ).one_or_none()
        if root_row is None or root_row.state not in VISIBLE_STATES:
            return CommentSubListResp(
                items=[],
                root=str(root),
                total=0,
                page_num=page_num,
                page_size=page_size,
            )

        stmt = (
            select(CommentIndex)
            .where(
                col(CommentIndex.root) == root,
                col(CommentIndex.oid) == oid,
                col(CommentIndex.type) == type_,
                col(CommentIndex.state).in_(VISIBLE_STATES),
            )
            .order_by(col(CommentIndex.rpid))
        )
        offset = (page_num - 1) * page_size
        rows = (await session.exec(stmt.offset(offset).limit(page_size))).all()
        items = await CommentReadService.assemble_items(
            session, rows, viewer_mid=viewer_mid
        )
        return CommentSubListResp(
            items=items,
            root=str(root),
            total=root_row.rcount,
            page_num=page_num,
            page_size=page_size,
        )

    # ==================== 视图组装 ====================

    @staticmethod
    async def assemble_items(
        session: AsyncSession,
        rows: list[CommentIndex],
        *,
        viewer_mid: int | None = None,
    ) -> list[CommentItem]:
        """把索引行批量组装成完整视图模型。

        这是全模块唯一的装配入口：一级列表、楼中楼、详情都复用它，
        保证「批量回捞」的约束不会在某个分支上被绕过。
        """
        if not rows:
            return []

        rpids = [r.rpid for r in rows]

        # ---- ① 正文批量回捞 ----
        content_rows = (
            await session.exec(
                select(CommentContent).where(col(CommentContent.rpid).in_(rpids))
            )
        ).all()
        contents: dict[int, CommentContent] = {c.rpid: c for c in content_rows}

        # ---- ② 汇总本页涉及的全部 mid，一次取回用户快照 ----
        mids: set[int] = set()
        for row in rows:
            mids.add(row.mid)
            if row.reply_to_mid:
                mids.add(row.reply_to_mid)
        for content in content_rows:
            mids.update(content.at_mids or [])
        # 用户展示信息统一从 pptr Postgres 只读取回（本服务不再冗余快照）。
        # 一次 IN 查询取齐本页全部 mid，SQL 次数与评论条数无关。
        profiles = await PptrUserService.get_many(mids)

        # ---- ③ 当前登录用户对本页评论的互动态 ----
        actions: dict[int, CommentActionEnum] = {}
        if viewer_mid:
            action_rows = (
                await session.exec(
                    select(CommentAction).where(
                        col(CommentAction.rpid).in_(rpids),
                        col(CommentAction.mid) == viewer_mid,
                    )
                )
            ).all()
            actions = {a.rpid: a.action for a in action_rows}

        return [
            CommentReadService._build_item(
                row,
                content=contents.get(row.rpid),
                profiles=profiles,
                action=actions.get(row.rpid, CommentActionEnum.NONE),
            )
            for row in rows
        ]

    @staticmethod
    def _build_item(
        row: CommentIndex,
        *,
        content: CommentContent | None,
        profiles: dict[int, CommentUserBrief],
        action: CommentActionEnum,
    ) -> CommentItem:
        """把「索引行 + 正文行 + 用户快照」拼成一条视图模型。"""
        at_users = [
            profiles[at_mid]
            for at_mid in (content.at_mids if content else [])
            if at_mid in profiles
        ]
        ip_v4_masked, ip_v6_masked = mask_ip_pair(
            content.ip_v4 if content else None,
            content.ip_v6 if content else None,
        )
        return CommentItem(
            rpid=str(row.rpid),
            oid=str(row.oid),
            type=row.type,
            mid=row.mid,
            member=profiles.get(row.mid),
            root=str(row.root),
            parent=str(row.parent),
            dialog=str(row.dialog),
            floor=row.floor,
            reply_to=profiles.get(row.reply_to_mid) if row.reply_to_mid else None,
            message=content.message if content else "",
            pictures=list(content.pictures) if content else [],
            at_users=at_users,
            emote_meta=content.emote_meta if content else None,
            like_count=row.like_count,
            hate_count=row.hate_count,
            rcount=row.rcount,
            action=action,
            state=row.state,
            is_top=bool(row.attr & CommentAttrBit.TOP.value),
            is_essence=bool(row.attr & CommentAttrBit.ESSENCE.value),
            is_up_liked=bool(row.attr & CommentAttrBit.UP_LIKED.value),
            ip_v4_masked=ip_v4_masked,
            ip_v6_masked=ip_v6_masked,
            plat=content.plat if content else None,
            device=content.device if content else None,
            ctime=row.created_at,
            # 楼中楼预览在 Phase 2 填充（批量查询后按 root 分组截断）
            replies=[],
        )


__all__ = ["CommentReadService", "CommentStateEnum"]
