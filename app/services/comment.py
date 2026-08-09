"""评论写服务：发布与删除。

对应 B 站评论架构中的 `reply-service`（基础服务层）写路径部分。

一次发布在**同一个事务**内完成四件事：

1. 写索引行 `msg_comment_index`（轻量、供列表查询）
2. 写正文行 `msg_comment_content`（大字段隔离）
3. 原子累加评论区计数 `msg_comment_subject`
4. 楼中楼时再原子累加根评论的 `rcount`

第 3、4 步一律使用 `UPDATE ... SET c = c + 1` 的**数据库侧原子自增**，
而不是「读到内存 +1 再写回」—— 后者在并发下必然丢更新。

计数为什么冗余而不实时 COUNT：评论区动辄上万条，
列表接口每次 `COUNT(*)` 会扫掉整个索引区间，是最典型的性能陷阱。
代价是极端并发下计数可能漂移，由 Phase 5 的对账定时任务兜底。
"""

import asyncio
import hashlib
import re
import time
from datetime import datetime
from urllib.parse import urlparse

from loguru import logger
from sqlalchemy import update
from sqlmodel import col, select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.core.config import settings
from app.core.database import new_session
from app.core.sharding import generate_msgkey
from app.models.db import (
    CommentAt,
    CommentContent,
    CommentIndex,
    CommentSubject,
)
from app.models.enums import (
    CommentAttrBit,
    CommentStateEnum,
    CommentSubjectStateEnum,
    CommentTypeEnum,
    EventTypeEnum,
    NotifyLevelEnum,
    SourceTypeEnum,
)
from app.models.schemas import CommentAddReq, CommentAddResp
from app.services.comment_audit import audit_text
from app.services.follow import FollowService
from app.services.notify import NotifyService
from app.utils.audit_source import build_comment_source
from app.utils.notify_markup import markup_inline_link
from app.utils.ua_parse import parse_user_agent

# 管理端未填写原因时的兜底文案
DEFAULT_REJECT_REASON = "评论内容违反社区规范"
# 通知正文里引用原文的最大长度，过长截断避免撑爆通知
_EXCERPT_LIMIT = 60


def summarize_text(text: str, limit: int = _EXCERPT_LIMIT) -> str:
    """把评论原文压成单行摘要，供通知正文引用。"""
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else f"{flat[:limit]}…"

# 正文里 @提及的占位符写法：@{114514}
_AT_PATTERN = re.compile(r"@\{(\d{1,19})\}")

# 列表中「对所有人可见」的状态集合
VISIBLE_STATES: tuple[CommentStateEnum, ...] = (CommentStateEnum.NORMAL,)

# ==================== 防刷（Phase 2.6）====================
# 同用户、相同正文，10s 内最多 3 次。进程内近似实现（单实例够用）；
# 多实例下各实例独立计数，可接受——极端刷评由 Phase 5 审核 / 对账兜底。
_RATE_LIMIT_WINDOW = 10
_RATE_LIMIT_MAX = 3
_rate_lock = asyncio.Lock()
_rate_history: dict[int, list[tuple[float, str]]] = {}


async def _check_rate_limit(mid: int, message: str) -> None:
    """进程内防刷：同用户同内容 10s 内超过 3 次则拒绝。"""
    key = hashlib.md5((message or "").encode("utf-8")).hexdigest()
    now = time.time()
    async with _rate_lock:
        hist = _rate_history.setdefault(mid, [])
        # 丢弃窗口外的旧记录
        hist[:] = [(t, k) for (t, k) in hist if now - t < _RATE_LIMIT_WINDOW]
        same_count = sum(1 for (t, k) in hist if k == key)
        if same_count >= _RATE_LIMIT_MAX:
            raise ValueError("操作过于频繁，请稍后再试")
        hist.append((now, key))


def generate_rpid() -> int:
    """生成评论 id。

    直接复用 `app.core.sharding` 的雪花生成器：全局唯一、单调递增、
    高位内嵌毫秒时间戳。单调性带来一个额外好处 —— 「按 rpid 倒序」
    天然等价于「按发布时间倒序」，时间排序不需要再建时间索引。
    """
    return generate_msgkey()


def parse_at_mids(message: str, declared: list[int] | None = None) -> list[int]:
    """解析正文中的 @提及，与前端声明的列表取并集后去重。

    以**正文里的占位符为准**，前端传入的 `at_mids` 只作补充：
    否则客户端可以只在 at_mids 里塞 mid 而正文不含占位符，
    变成一个静默的「群发骚扰」通道。
    """
    found = [int(m) for m in _AT_PATTERN.findall(message or "")]
    merged: list[int] = []
    seen: set[int] = set()
    for mid in [*found, *(declared or [])]:
        if mid > 0 and mid not in seen:
            seen.add(mid)
            merged.append(mid)
    return merged


def _to_int_id(raw: str | int | None, default: int = 0) -> int:
    """接口层的字符串 ID → int，非法值回落到默认值。"""
    if raw is None:
        return default
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def _validate_pictures(pictures: list[str]) -> list[str]:
    """校验图片 URL 列表。

    只存 URL、不做本地转存，所以服务端能做的把关只有三件事：
    数量上限、URL 合法性、域名白名单（配置为空时不限制）。
    """
    if not pictures:
        return []
    if len(pictures) > settings.comment_picture_max:
        raise ValueError(f"图片最多 {settings.comment_picture_max} 张")

    whitelist = [d.lower() for d in settings.comment_picture_domains if d]
    cleaned: list[str] = []
    for raw in pictures:
        url = (raw or "").strip()
        if not url:
            continue
        if len(url) > 512:
            raise ValueError("图片URL长度不能超过512字符")
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise ValueError(f"非法的图片URL: {url}")
        if whitelist:
            host = parsed.hostname.lower()
            if not any(host == d or host.endswith(f".{d}") for d in whitelist):
                raise ValueError(f"图片域名不在白名单内: {host}")
        cleaned.append(url)
    return cleaned


class CommentService:
    """评论的发布与删除。"""

    # ==================== 评论区 ====================

    @staticmethod
    async def get_subject(
        session: AsyncSession, oid: int, type_: CommentTypeEnum
    ) -> CommentSubject | None:
        stmt = select(CommentSubject).where(
            CommentSubject.oid == oid, CommentSubject.type == type_
        )
        return (await session.exec(stmt)).one_or_none()

    @staticmethod
    async def get_or_create_subject(
        session: AsyncSession,
        oid: int,
        type_: CommentTypeEnum,
        up_mid: int = 0,
    ) -> CommentSubject:
        """取评论区，不存在则惰性创建。

        评论区不需要业务方预先注册：第一条评论到来时自动开区，
        少一次跨服务的「创建评论区」调用。
        """
        row = await CommentService.get_subject(session, oid, type_)
        if row is not None:
            # 首次开区时 up_mid 可能还不知道，后续补齐
            if not row.up_mid and up_mid:
                row.up_mid = up_mid
                session.add(row)
                await session.commit()
                await session.refresh(row)
            return row

        row = CommentSubject(oid=oid, type=type_, up_mid=up_mid)
        session.add(row)
        try:
            await session.commit()
            await session.refresh(row)
        except Exception:
            # 并发下可能被别的请求抢先建区，回滚重读即可
            await session.rollback()
            existing = await CommentService.get_subject(session, oid, type_)
            if existing is None:
                raise
            row = existing
        return row

    # ==================== 发布 ====================

    @staticmethod
    async def add(
        session: AsyncSession,
        mid: int,
        req: CommentAddReq,
        *,
        uname: str | None = None,
        ip_v4: str | None = None,
        ip_v6: str | None = None,
        user_agent: str | None = None,
    ) -> CommentAddResp:
        """发表一条评论（一级或楼中楼）。

        Raises:
            ValueError: 参数非法 / 评论区已关闭 / 父评论不存在等业务校验失败。
        """
        oid = _to_int_id(req.oid)
        if oid <= 0:
            raise ValueError("oid 不合法")

        # 防刷：同用户同内容 10s 内超过阈值直接拒绝（Phase 2.6）
        await _check_rate_limit(mid, req.message)

        message = (req.message or "").strip()
        if not message:
            raise ValueError("评论内容不能为空")
        if len(message) > settings.comment_message_max_length:
            raise ValueError(
                f"评论内容最长 {settings.comment_message_max_length} 个字"
            )

        pictures = _validate_pictures(req.pictures)
        at_mids = parse_at_mids(message, req.at_mids)
        if len(at_mids) > settings.comment_at_max:
            raise ValueError(f"单条评论最多 @ {settings.comment_at_max} 人")

        subject = await CommentService.get_or_create_subject(
            session, oid, req.type, up_mid=req.up_mid or 0
        )
        if subject.state is CommentSubjectStateEnum.CLOSED:
            raise ValueError("该评论区已关闭，无法发表评论")

        root = _to_int_id(req.root)
        parent = _to_int_id(req.parent)
        root, parent, dialog, reply_to_mid = await CommentService._resolve_tree(
            session, subject, root, parent
        )

        # ---- 拉黑拦截：被拉黑者不能在对方评论区发言、也不能直接回复对方 ----
        # 评论区作者（up_mid）拉黑了我 → 不能在该评论区发任何评论
        up_mid = subject.up_mid or 0
        if up_mid > 0 and up_mid != mid:
            if await FollowService.is_blocked_by(session, mid, up_mid):
                raise ValueError("对方已拉黑你，无法评论")
        # 楼中楼场景：reply_to_mid（回复对象）拉黑了我 → 不能直接回复对方
        if reply_to_mid > 0 and reply_to_mid != up_mid and reply_to_mid != mid:
            if await FollowService.is_blocked_by(session, mid, reply_to_mid):
                raise ValueError("对方已拉黑你，无法回复")

        rpid = generate_rpid()
        now = datetime.now()

        # ---- 内容审核（Phase 5.1，强依赖）：命中高危直接 rejected，疑似置 auditing ----
        audit_state, _hit_words = audit_text(message)
        # 全局「先审后发」开关：开启后，原本可直接展示的评论也先进入审核态，
        # 对外不可见，需管理端审核通过（置 NORMAL）后才展示。命中高危词(REJECTED)不变。
        if settings.comment_pre_audit and audit_state is CommentStateEnum.NORMAL:
            audit_state = CommentStateEnum.AUDITING
        need_audit = audit_state != CommentStateEnum.NORMAL

        # ---- 计数 + 楼层发号：数据库侧原子自增后回读楼层号 ----
        # 仅「对外可见（NORMAL）」的评论才计入评论区冗余计数；
        # 审核中 / 已驳回的评论不计入（对应 VISIBLE_STATES 与对账任务口径）。
        subject_values: dict = {
            "floor_seq": col(CommentSubject.floor_seq) + 1,
        }
        if audit_state is CommentStateEnum.NORMAL:
            subject_values["all_count"] = col(CommentSubject.all_count) + 1
            if root == 0:
                subject_values["root_count"] = col(CommentSubject.root_count) + 1
        await session.exec(  # type: ignore[call-overload]
            update(CommentSubject)
            .where(col(CommentSubject.id) == subject.id)
            .values(**subject_values)
        )
        # 回读最新 floor_seq（避免内存对象滞后）
        subject = (
            await session.exec(
                select(CommentSubject).where(col(CommentSubject.id) == subject.id)
            )
        ).one()

        plat, device = parse_user_agent(user_agent)

        index_row = CommentIndex(
            rpid=rpid,
            oid=oid,
            type=req.type,
            mid=mid,
            root=root,
            parent=parent,
            dialog=dialog,
            reply_to_mid=reply_to_mid,
            floor=subject.floor_seq,
            state=audit_state,
            hot_score=0.0,
            created_at=now,
            updated_at=now,
        )
        content_row = CommentContent(
            rpid=rpid,
            message=message,
            pictures=pictures,
            at_mids=at_mids,
            emote_meta=req.emote_meta,
            ip_v4=ip_v4,
            ip_v6=ip_v6,
            plat=plat,
            device=device,
            created_at=now,
            updated_at=now,
        )
        session.add(index_row)
        session.add(content_row)

        if root != 0:
            await session.exec(  # type: ignore[call-overload]
                update(CommentIndex)
                .where(col(CommentIndex.rpid) == root)
                .values(rcount=col(CommentIndex.rcount) + 1)
            )

        # ---- @关系：uq(rpid, at_mid) 保证同一人只通知一次 ----
        for at_mid in at_mids:
            if at_mid == mid:
                # 不给自己发 @ 提醒
                continue
            session.add(
                CommentAt(
                    rpid=rpid,
                    oid=oid,
                    type=req.type,
                    from_mid=mid,
                    at_mid=at_mid,
                    created_at=now,
                    updated_at=now,
                )
            )

        await session.commit()

        # 用户展示信息（昵称 / 等级 / 大会员等）不在本服务落库：
        # 用户主数据只有一份，在 pptr 的 Postgres，读评论列表时按需只读取回。

        # ---- 弱依赖：通知（回复 / @ / 审核中 / 审核驳回），失败只告警不影响发评 ----
        try:
            if audit_state == CommentStateEnum.REJECTED:
                await CommentService.notify_audit_rejected(
                    mid, rpid, oid, req.type,
                    reason="评论内容包含违规或敏感词",
                    excerpt=message,
                )
            elif audit_state == CommentStateEnum.AUDITING:
                # 进入审核态（命中疑似词 / 开启「先审后发」）：告知作者正在审核
                source = build_comment_source(oid, CommentTypeEnum(req.type), rpid)
                source_link = markup_inline_link(
                    source.label, source.url or source.external_url
                )
                lines = [
                    f"您在{source_link}发布的评论正在审核中，通过后将对外公开展示。",
                    f"评论内容：{summarize_text(message)}",
                ]
                await NotifyService.send_to_user(
                    mid,
                    title="评论审核中",
                    content="\n".join(lines),
                    jump_url=source.url or source.external_url,
                )
            if root != 0 and reply_to_mid and reply_to_mid != mid:
                await CommentService._notify_reply(
                    mid, oid, rpid, reply_to_mid, message, uname
                )
            for at_mid in at_mids:
                if at_mid != mid:
                    await CommentService._notify_at(
                        mid, oid, rpid, at_mid, uname
                    )
        except Exception as e:  # noqa: BLE001
            logger.warning(f"评论通知投递失败（弱依赖，已忽略）: {e}")

        logger.debug(
            f"用户 {mid} 发表评论 rpid={rpid} oid={oid} type={req.type} root={root} "
            f"audit={audit_state}"
        )
        return CommentAddResp(
            rpid=str(rpid),
            root=str(root),
            parent=str(parent),
            state=audit_state,
            need_audit=need_audit,
        )

    @staticmethod
    async def notify_audit_rejected(
        author_mid: int,
        rpid: int,
        oid: int,
        ctype: int,
        reason: str | None = None,
        creator_mid: int = 0,
        excerpt: str | None = None,
    ) -> None:
        """评论未通过审核时，向作者发送系统通知（通过不通知）。

        用于「自动预筛命中高危词」与「管理端人工驳回」两条路径：
        评论状态变为 REJECTED 时调用。状态变为 NORMAL（通过 / 恢复）不通知。

        通知需让作者能自查「哪里的什么内容因为什么被打回」，因此正文包含三段：
        来源评论区（`build_comment_source` 的展示名）、原文摘要、驳回原因，
        并把可跳转地址挂到 `jump_url` 上，便于前端点通知直达出事的评论区。

        独立事务写入，与审核主流程解耦：通知失败不影响审核结果本身。
        """
        source = build_comment_source(oid, CommentTypeEnum(ctype), rpid)
        source_link = markup_inline_link(source.label, source.url or source.external_url)

        lines = [f"您在{source_link}发布的评论未通过审核，已被驳回。"]
        if excerpt:
            lines.append(f"评论内容：{summarize_text(excerpt)}")
        lines.append(f"驳回原因：{reason or DEFAULT_REJECT_REASON}")

        await NotifyService.send_to_user(
            author_mid,
            title="评论审核未通过",
            content="\n".join(lines),
            level=NotifyLevelEnum.IMPORTANT,
            jump_url=source.url or source.external_url,
            creator_mid=creator_mid,
        )

    @staticmethod
    async def _resolve_tree(
        session: AsyncSession,
        subject: CommentSubject,
        root: int,
        parent: int,
    ) -> tuple[int, int, int, int]:
        """校验并推导评论的树形关系。

        Returns:
            `(root, parent, dialog, reply_to_mid)`

        B 站的楼中楼**只有两层**：所有回复都挂在同一个根评论下，
        再深的回复通过 `reply_to_mid` 展示成「回复 @xxx」，
        而不是真的继续嵌套。这样列表渲染的层级是恒定的，
        不会出现无限缩进，查询也只需要 `WHERE root = ?` 一层。
        """
        if root == 0 and parent == 0:
            # 一级评论
            return 0, 0, 0, 0
        if root == 0:
            raise ValueError("root 与 parent 必须同时提供")

        root_row = (
            await session.exec(
                select(CommentIndex).where(col(CommentIndex.rpid) == root)
            )
        ).one_or_none()
        if root_row is None or root_row.state not in VISIBLE_STATES:
            raise ValueError("根评论不存在或已被删除")
        if root_row.oid != subject.oid or root_row.type != subject.type:
            raise ValueError("根评论不属于该评论区")
        if root_row.root != 0:
            raise ValueError("root 必须是一级评论")

        if parent == 0 or parent == root:
            # 直接回复一级评论：开启一条新的楼中楼会话串
            return root, root, root, root_row.mid

        parent_row = (
            await session.exec(
                select(CommentIndex).where(col(CommentIndex.rpid) == parent)
            )
        ).one_or_none()
        if parent_row is None or parent_row.state not in VISIBLE_STATES:
            raise ValueError("父评论不存在或已被删除")
        if parent_row.root != root:
            raise ValueError("父评论与 root 不匹配")

        # 继承父评论所在的会话串，保证「只看该对话」能串起完整上下文
        dialog = parent_row.dialog or root
        return root, parent, dialog, parent_row.mid

    # ==================== 删除 ====================

    @staticmethod
    async def delete(
        session: AsyncSession,
        mid: int,
        rpid: int,
        *,
        is_admin: bool = False,
    ) -> tuple[int, str]:
        """删除评论（软删）。

        软删而非物理删除的原因：楼中楼引用了根评论的 rpid，
        物理删除会让子评论变成孤儿；保留行可以渲染成
        「该评论已被删除」的占位，上下文不断裂。

        允许删除的角色：评论作者本人 / 内容作者（UP 主）/ 管理员。

        Returns:
            `(affected, message)`
        """
        row = (
            await session.exec(
                select(CommentIndex).where(col(CommentIndex.rpid) == rpid)
            )
        ).one_or_none()
        if row is None:
            return 0, "评论不存在"
        if row.state is CommentStateEnum.DELETED:
            return 0, "评论已删除"

        subject = await CommentService.get_subject(session, row.oid, row.type)
        allowed = is_admin or row.mid == mid or (
            subject is not None and subject.up_mid and subject.up_mid == mid
        )
        if not allowed:
            return 0, "无权删除该评论"

        # 先记下原始状态：只有「曾计入冗余计数」的可见评论才回减计数
        was_visible = row.state is CommentStateEnum.NORMAL
        row.state = CommentStateEnum.DELETED
        # 置顶评论被删时同步清掉置顶态，避免列表顶部出现空洞
        row.attr = row.attr & ~CommentAttrBit.TOP.value
        session.add(row)

        if subject is not None:
            subject_values: dict = {}
            # 仅撤销「曾计入冗余计数」的可见评论；审核中/已驳回从未计数，不回减
            if was_visible:
                subject_values["all_count"] = col(CommentSubject.all_count) - 1
                if row.root == 0:
                    subject_values["root_count"] = col(CommentSubject.root_count) - 1
            if subject.top_rpid == rpid:
                subject_values["top_rpid"] = None
            if subject_values:
                await session.exec(  # type: ignore[call-overload]
                    update(CommentSubject)
                    .where(col(CommentSubject.id) == subject.id)
                    .values(**subject_values)
                )

        if row.root != 0:
            await session.exec(  # type: ignore[call-overload]
                update(CommentIndex)
                .where(col(CommentIndex.rpid) == row.root)
                .values(rcount=col(CommentIndex.rcount) - 1)
            )

        await session.commit()
        logger.debug(f"用户 {mid} 删除评论 rpid={rpid}（admin={is_admin}）")
        return 1, "已删除"

    # ==================== 置顶（Phase 3.5）====================

    @staticmethod
    async def set_top(
        session: AsyncSession,
        mid: int,
        oid: int,
        type_: CommentTypeEnum,
        rpid: int,
        *,
        is_admin: bool = False,
        top: bool = True,
    ) -> bool:
        """置顶 / 取消置顶一条根评论。

        权限：内容作者（subject.up_mid）或管理员。全区唯一一条置顶，
        互斥覆盖——新置顶会清掉旧置顶的 TOP 位标记。

        Returns:
            是否成功（评论不存在 / 非根评论 / 无权限均返回 False）。
        """
        subject = await CommentService.get_subject(session, oid, type_)
        if subject is None:
            return False
        row = (
            await session.exec(
                select(CommentIndex).where(col(CommentIndex.rpid) == rpid)
            )
        ).one_or_none()
        if row is None or row.state not in VISIBLE_STATES:
            return False
        if row.root != 0:
            # 只有一级评论可置顶
            return False
        allowed = is_admin or (subject.up_mid and subject.up_mid == mid)
        if not allowed:
            return False

        # 清掉旧置顶的 TOP 位（若与本次不同）
        if subject.top_rpid and subject.top_rpid != rpid:
            old = (
                await session.exec(
                    select(CommentIndex).where(
                        col(CommentIndex.rpid) == subject.top_rpid
                    )
                )
            ).one_or_none()
            if old is not None:
                old.attr = old.attr & ~CommentAttrBit.TOP.value
                session.add(old)

        if top:
            subject.top_rpid = rpid
            row.attr = row.attr | CommentAttrBit.TOP.value
        else:
            subject.top_rpid = None
            row.attr = row.attr & ~CommentAttrBit.TOP.value
        session.add(row)
        session.add(subject)
        await session.commit()
        return True

    # ==================== 通知（Phase 3.3，弱依赖）====================

    @staticmethod
    async def _notify_reply(
        actor_mid: int,
        oid: int,
        rpid: int,
        to_mid: int,
        message: str,
        actor_uname: str | None = None,
    ) -> None:
        """弱依赖：通知被回复者。

        独立会话投递：即便事件落库失败，也绝不污染「发评」主事务的会话。
        """
        from app.services.event import EventService
        from loguru import logger

        try:
            async with new_session() as ns:
                await EventService.report(
                    ns,
                    EventReportReq(
                        mid=to_mid,
                        event_type=EventTypeEnum.REPLY,
                        source_type=SourceTypeEnum.COMMENT,
                        source_id=str(oid),
                        actor_mid=actor_mid,
                        actor_name=actor_uname,
                        content=message,
                        biz_id=str(rpid),
                    ),
                )
        except Exception as e:  # noqa: BLE001
            logger.warning(f"回复通知投递失败（弱依赖，已忽略）: {e}")

    @staticmethod
    async def _notify_at(
        actor_mid: int,
        oid: int,
        rpid: int,
        to_mid: int,
        actor_uname: str | None = None,
    ) -> None:
        """弱依赖：通知被 @ 者。

        独立会话投递：失败不影响发评主流程。
        """
        from app.services.event import EventService
        from loguru import logger

        try:
            async with new_session() as ns:
                await EventService.report(
                    ns,
                    EventReportReq(
                        mid=to_mid,
                        event_type=EventTypeEnum.AT,
                        source_type=SourceTypeEnum.COMMENT,
                        source_id=str(oid),
                        actor_mid=actor_mid,
                        actor_name=actor_uname,
                        biz_id=str(rpid),
                    ),
                )
        except Exception as e:  # noqa: BLE001
            logger.warning(f"@通知投递失败（弱依赖，已忽略）: {e}")


__all__ = [
    "CommentService",
    "generate_rpid",
    "parse_at_mids",
    "VISIBLE_STATES",
]
