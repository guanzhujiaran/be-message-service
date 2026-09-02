"""动态发布服务：发布 CRUD + 审核状态流转（Phase 2）。

覆盖 P2-T2 ~ P2-T6：

- 纯文字动态创建（WORD）：auditStatus 默认 ``auditing``，正文存富文本节点 +
  去标签纯文本 ``contentText``（便于全文搜索）。
- 转发动态创建（FORWARD）：校验源动态 ``auditStatus='normal'``；写 ``repostSrcDynId``
  + 转发深度；**创建时不 +repostCount**，等管理员审核通过（P6-T2）再加 1。
- 编辑 / 删除：编辑 rejected / auditing 动态 → 重置 ``auditing`` 重新审核；
  软删（设 ``deletedAt``）；若被操作的 FORWARD 动态 ``before=normal``，则对
  ``repostSrcDynId`` 指向的源动态 ``repostCount -1``（状态机触发点 ④）。
- 空间置顶 / 取消置顶：仅本人 + ``normal`` 状态可操作。
- 发布前置校验：MVP 仅允许 WORD / FORWARD；字数上限、@ 数量上限、权限校验。

所有写操作统一在同一事务内提交（``session.add`` + ``await session.commit()``），
父子链（TMoment → TInteractionStat/TResourceFeed）插入顺序遵循计划书 §6.3：
先 flush 父行再插子行。
"""

from datetime import datetime
from typing import Any

from loguru import logger
from sqlmodel import col, delete, select, update
from sqlmodel.ext.asyncio.session import AsyncSession

from app.core.database import new_session
from app.core.sharding import generate_moment_id
from app.models.db import (
    TMoment,
    TResourceAuditLog,
    TMomentTopic,
    TMomentTopicRel,
    TResourceFeed,
    )
from bili_common.models import InteractionActionTypeEnum, InteractionBizTypeEnum
from app.models.enums import (
    MomentAuditLogActionEnum,
    MomentAuditLogOperatorRoleEnum,
    MomentAuditStatusEnum,
    MomentTopicAuditStatusEnum,
    MomentTypeEnum,
    MomentVisibleScopeEnum,
)
from app.models.schemas import EventReportReq
from app.models.schemas.moment import (
    MomentAttachRef,
    MomentContentNode,
    MomentCreateReq,
    MomentEditReq,
    MomentRemoveReq,
    MomentRepostReq,
    MomentTopicRef,
    MomentTopReq,
    )
from app.services.user.account import PptrUser

# 业务上限（MVP）
_CONTENT_MAX_LENGTH = 2000
_AT_MAX_COUNT = 10
# 单条动态最多允许的图片数（renderAsImage 的 LINK 节点）；前端九宫格只展示 9 张，
# 超出部分收起为「更多图片」，最多 18 张
_MAX_IMAGE_COUNT = 18
# 单条动态最多允许关联的话题数（2.22.0，去重后计数）
_MAX_TOPIC_COUNT = 5

# MVP 允许的动态场景
_ALLOWED_SCENES = {"WORD", "FORWARD"}


# ==================== 工具函数 ====================


def _nodes_to_text(nodes: list[MomentContentNode] | None) -> str:
    """把富文本节点列表压成纯文本（去标签），用于 contentText 全文检索。"""
    if not nodes:
        return ""
    parts: list[str] = []
    for n in nodes:
        if n.type == "WORDS":
            parts.append(n.text or "")
        elif n.type == "AT":
            parts.append(f"@{n.name}" if n.name else "@")
        elif n.type == "TOPIC":
            parts.append(f"#{n.name}#" if n.name else "#")
        elif n.type == "LINK":
            parts.append(n.text or (n.jumpUrl or ""))
        elif n.type == "RESOURCE":
            # 资源引用：正文展示资源名（若为空则忽略，不影响纯文本）
            parts.append(n.name or "")
    return "".join(parts).strip()


def _count_at(nodes: list[MomentContentNode] | None) -> int:
    """统计正文中 @ 节点数量。"""
    if not nodes:
        return 0
    return sum(1 for n in nodes if n.type == "AT")


def _to_enum_dyn_type(scene: str) -> MomentTypeEnum:
    """把请求里的 scene 字符串解析成 MomentTypeEnum。"""
    try:
        return MomentTypeEnum[scene]
    except KeyError as exc:
        raise ValueError(f"不支持的动态类型: {scene}") from exc


def _build_audit_log(
    *,
    biz_type: InteractionBizTypeEnum,
    biz_id: int,
    operator_mid: int,
    to_status: MomentAuditStatusEnum,
    action: MomentAuditLogActionEnum,
    from_status: MomentAuditStatusEnum | None = None,
    reject_reason: str | None = None,
    remark: str | None = None,
    operator_role: MomentAuditLogOperatorRoleEnum = MomentAuditLogOperatorRoleEnum.AUTHOR,
    client_ip: str | None = None,
    user_agent: str | None = None,
) -> TResourceAuditLog:
    """构造一条通用资源审核流转记录（2.55.0 起，`ResourceBase` 子表）。

    2.22.1 ``operator_role``：操作人角色，默认作者（author）；
    管理员删除等管理端操作传 ``ADMIN``。
    2.55.0：传 `biz_type` + `biz_id`（动态资源 = `InteractionBizTypeEnum.DYNAMIC + dynId`）。
    """
    return TResourceAuditLog(
        bizType=biz_type,
        bizId=biz_id,
        mid=operator_mid,
        operatorRole=operator_role,
        fromStatus=from_status,
        toStatus=to_status,
        actionType=action,
        rejectReason=reject_reason,
        remark=remark,
        clientIp=client_ip,
        userAgent=user_agent,
    )


def _to_base_resp(dyn: TMoment) -> dict[str, Any]:
    """把 TMoment 行转换为接口出参字典（含 dynId + dynIdStr 双形态）。"""
    return {
        "dynId": dyn.dynId,
        "dynIdStr": str(dyn.dynId),
        "auditStatus": dyn.auditStatus.name,
        "dynType": dyn.dynType.name,
    }


# ==================== 前置校验（P2-T6）====================


def _precheck_scene(scene: str) -> MomentTypeEnum:
    """发布前置校验：场景合法性 + MVP 范围。"""
    if scene not in _ALLOWED_SCENES:
        raise ValueError(f"暂不支持动态类型 {scene}，MVP 仅支持 WORD / FORWARD")
    return _to_enum_dyn_type(scene)


def _precheck_content(nodes: list[MomentContentNode] | None) -> None:
    """发布前置校验：字数上限 + @ 数量上限 + 图片数量上限 + 图片必须为站外 http(s) 链接。"""
    if not nodes:
        raise ValueError("动态正文不能为空")
    text = _nodes_to_text(nodes)
    if not text:
        raise ValueError("动态正文不能为空")
    if len(text) > _CONTENT_MAX_LENGTH:
        raise ValueError(f"动态正文最长 {_CONTENT_MAX_LENGTH} 个字")
    if _count_at(nodes) > _AT_MAX_COUNT:
        raise ValueError(f"单条动态最多 @ {_AT_MAX_COUNT} 人")

    image_urls = [
        n.jumpUrl
        for n in nodes
        if n.type == "LINK" and n.picMeta and bool(n.picMeta.get("renderAsImage", False))
    ]
    if len(image_urls) > _MAX_IMAGE_COUNT:
        raise ValueError(f"单条动态最多 {_MAX_IMAGE_COUNT} 张图片")
    # 产品约束：不允许本地上传/内嵌 dataURL，图片必须引用站外 http(s) 链接
    for url in image_urls:
        if not url or not (url.startswith("http://") or url.startswith("https://")):
            raise ValueError("图片必须是站外 http(s) 链接，不支持本地上传")

    # 2.21.0：正文不再承载 RESOURCE 节点（attach 卡改走独立 attach 字段）；
    # 若旧客户端仍传入，兼容提升为 attach（见 _resolve_attach），此处不再强校验


# ==================== 查询辅助 ====================


def _resolve_attach(
    req: MomentCreateReq,
) -> tuple[MomentAttachRef | None, list[MomentContentNode]]:
    """解析发布/编辑请求的 attach 附加卡（2.21.0）。

    - 优先取 `req.attach`（新客户端，只传 bizType+bizId）；
    - 若未提供，兼容旧客户端：把正文中第一个 RESOURCE 节点提升为 attach，
      并从正文节点中移除（不重复保存）。

    Returns:
        (attach, 剥离 RESOURCE 后的正文节点)
    """
    nodes = list(req.content or [])
    if req.attach and req.attach.bizType and req.attach.bizId:
        return req.attach, nodes
    for i, n in enumerate(nodes):
        if n.type == "RESOURCE" and n.bizType and n.bizId:
            attach = MomentAttachRef(bizType=n.bizType, bizId=n.bizId)
            return attach, [x for j, x in enumerate(nodes) if j != i]
    return None, nodes


async def _validate_attach(
    session: AsyncSession, attach: MomentAttachRef | None
) -> None:
    """校验附加卡资源存在（2.21.0）。

    经对应资源类的 ``check_exists`` 校验存在，不存在抛 ValueError（路由层转 422）；
    未实现资源类默认放行。
    """
    from app.services.interaction_actions.base_biz import get_biz

    if attach and attach.bizType and attach.bizId:
        try:
            biz = get_biz(attach.bizType, session, int(attach.bizId))
        except (ValueError, TypeError):
            return
        if not await biz.check_exists():
            raise ValueError("资源不存在")


def _resolve_topics(req: MomentCreateReq | MomentEditReq) -> list[MomentTopicRef]:
    """合并发布/编辑请求的多话题（2.22.0）。

    - 兼容字段：`req.topics`（新，数组）与 `req.topic`（旧，单话题）合并；
    - 按 `topicId` 去重（保持首次出现顺序）；
    - 数量超过 `_MAX_TOPIC_COUNT` 抛 ValueError（路由层转 400）。
    """
    merged: list[MomentTopicRef] = []
    seen: set[int] = set()
    sources = list(req.topics or [])
    if req.topic and req.topic.topicId:
        sources.append(req.topic)
    for t in sources:
        if not t.topicId:
            continue
        if t.topicId in seen:
            continue
        seen.add(t.topicId)
        merged.append(t)
    if len(merged) > _MAX_TOPIC_COUNT:
        raise ValueError(f"单条动态最多关联 {_MAX_TOPIC_COUNT} 个话题")
    return merged


async def _validate_topics(
    session: AsyncSession, topics: list[MomentTopicRef]
) -> list[MomentTopicRef]:
    """校验多话题均存在且已审核通过（auditStatus='normal'），否则 422。

    一次 IN 批量查询（无 N+1）；返回校验通过的话题列表（顺序不变）。
    """
    if not topics:
        return []
    topic_ids = [t.topicId for t in topics]
    rows = (
        await session.exec(
            select(TMomentTopic).where(col(TMomentTopic.topicId).in_(topic_ids))
        )
    ).all()
    by_id = {r.topicId: r for r in rows}
    valid: list[MomentTopicRef] = []
    for t in topics:
        row = by_id.get(t.topicId)
        if row is None or row.auditStatus is not MomentTopicAuditStatusEnum.NORMAL:
            raise ValueError("话题不存在或未通过审核")
        valid.append(t)
    return valid


async def _persist_topic_rels(
    session: AsyncSession, dyn_id: int, topic_ids: list[int]
) -> None:
    """批量写入动态-话题关系（2.22.0）。

    调用方需保证：`dyn_id` 对应 TMoment 父行已 flush；`topic_ids` 已去重、
    已通过 `_validate_topics` 校验（仅 normal 话题）。
    """
    for tid in topic_ids:
        session.add(TMomentTopicRel(dynId=dyn_id, topicId=tid))


def _persist_resource_feed(
    session: AsyncSession,
    moment_id: int,
    mid: int,
    topics: list[Any] | None,
    visible_scope: MomentVisibleScopeEnum | None = None,
) -> None:
    """发布时写通用 Feed 元数据行（2.36.0，计数统一 TInteractionStat）。

    初始状态：``auditStatus=auditing``、``pubTime=None``（审核通过后由
    ``moment_audit`` 写 pubTime 并置 normal）；``tags`` 冗余话题 id 列表，
    供推荐流个性化 / 话题流过滤；``visibleScope``（2.46.0）冗余可见范围，
    推荐流候选零 join 过滤 PUBLIC（WORD 传实际值、FORWARD 强制 PUBLIC）。
    """
    session.add(
        TResourceFeed(
            bizType=InteractionBizTypeEnum.DYNAMIC,
            bizId=moment_id,
            mid=mid,
            auditStatus="auditing",
            tags=[t.topicId for t in topics] if topics else [],
            visibleScope=visible_scope,
        )
    )


async def _get_dynamic_or_404(session: AsyncSession, moment_id: int) -> TMoment:
    """按 dynId 取动态主表行，不存在抛 ValueError（由路由层转 404/400）。"""
    row = (
        await session.exec(select(TMoment).where(col(TMoment.dynId) == moment_id))
    ).one_or_none()
    if row is None:
        raise ValueError("动态不存在")
    return row


def _resolve_ip_geo(
    client_ip: str | None,
) -> tuple[str | None, float | None, float | None, str | None, str | None]:
    """按客户端 IP 自动解析位置与属地（lbsPoi / lbsLat / lbsLng / ipLocation / ipIsp）。

    产品约定：位置与 IP 属地**由服务端根据请求 IP 自动计算**，不允许前端传入。
    因此这里不使用 ``req.lbs``，只依赖 GeoIP 属地库（City + ASN）；
    解析失败（内网 IP / mmdb 缺失等）时字段留空，不影响发布。
    """
    try:
        from app.services.infrastructure.geo_ip import lookup

        r = lookup(client_ip)
        # lookup 内部已用「未知」兜底（不抛异常），这里防御性保留 None 判断
        if r is None:
            return None, None, None, None, None
        return r.poi, r.lat, r.lng, r.poi, r.isp
    except Exception:  # noqa: BLE001 - 解析失败静默降级
        return None, None, None, None, None


# ==================== 创建：WORD / FORWARD（P2-T2 / P2-T3）====================


class MomentPublishService:
    """动态发布 CRUD + 审核流转服务（静态方法集合，无状态）。"""

    @staticmethod
    async def create(
        session: AsyncSession,
        mid: int,
        req: MomentCreateReq,
        *,
        client_ip: str | None = None,
        user_agent: str | None = None,
    ) -> dict[str, Any]:
        """创建一条动态（WORD 或 FORWARD）。

        新建动态一律 auditStatus='auditing'，pubTime=NULL，仅作者本人空间可见。
        """
        dyn_type = _precheck_scene(req.scene)
        attach, nodes = _resolve_attach(req)
        # 2.21.0：attach 卡只落 bizType+bizId，正文剥离 RESOURCE；校验资源存在（如 lottery），不存在 422
        _precheck_content(nodes)
        await _validate_attach(session, attach)
        # 2.22.0：多话题（合并 topics+topic 去重，数量≤5，逐一校验 normal）
        topics = await _validate_topics(session, _resolve_topics(req))

        if dyn_type is MomentTypeEnum.FORWARD:
            data = await MomentPublishService._create_forward(
                session, mid, req, nodes, attach=attach, topics=topics, client_ip=client_ip, user_agent=user_agent
            )
        else:
            data = await MomentPublishService._create_word(
                session, mid, req, nodes, attach=attach, topics=topics, client_ip=client_ip, user_agent=user_agent
            )
        # 弱依赖：发布时解析 @ 节点，批量生产 AT 事件（P6-T8）
        await MomentPublishService._notify_at_batch(mid, data["dynId"], nodes)
        return data

    @staticmethod
    async def _create_word(
        session: AsyncSession,
        mid: int,
        req: MomentCreateReq,
        nodes: list[MomentContentNode],
        attach: MomentAttachRef | None = None,
        topics: list[MomentTopicRef] | None = None,
        *,
        client_ip: str | None = None,
        user_agent: str | None = None,
    ) -> dict[str, Any]:
        moment_id = await generate_moment_id()
        now = datetime.now()
        content_text = _nodes_to_text(nodes)
        # 2.22.0：多话题——主话题写 TMoment.topicId（=topics[0]），全部写 TMomentTopicRel
        topic_id = topics[0].topicId if topics else None
        option = req.option or None
        # 位置与 IP 属地由服务端根据请求 IP 自动计算（不允许前端传入）
        lbs_poi, lbs_lat, lbs_lng, ip_location, ip_isp = _resolve_ip_geo(client_ip)

        dyn = TMoment(
            dynId=moment_id,
            mid=mid,
            dynType=MomentTypeEnum.WORD,
            contentText=content_text,
            contentJson=[n.model_dump() for n in nodes],
            # 2.21.0：attach 卡只落 bizType+bizId（复用预留 bizType/bizRid 列，无表结构变更；
            # bizType 为对外文字，落库前转 IntEnum）
            bizType=InteractionBizTypeEnum.from_text(attach.bizType) if attach and attach.bizType else None,
            bizRid=int(attach.bizId) if attach and attach.bizId else None,
            topicId=topic_id,
            lbsPoi=lbs_poi,
            lbsLat=lbs_lat,
            lbsLng=lbs_lng,
            ipLocation=ip_location,
            ipIsp=ip_isp,
            closeComment=option.closeComment if option else 0,
            # 2.46.0：WORD 可设可见范围（缺省 None → 模型默认 PUBLIC）
            visibleScope=option.visibleScope if option else None,
            auditStatus=MomentAuditStatusEnum.AUDITING,
            created_at=now,
            updated_at=now,
        )
        session.add(dyn)
        # 父子链：先 flush 父行，拿到 dynId 后再写 Feed 元数据行
        await session.flush()
        if topics:
            await _persist_topic_rels(session, moment_id, [t.topicId for t in topics])
        _persist_resource_feed(session, moment_id, mid, topics, visible_scope=dyn.visibleScope)
        session.add(
            _build_audit_log(
                biz_type=InteractionBizTypeEnum.DYNAMIC,
                biz_id=moment_id,
                operator_mid=mid,
                to_status=MomentAuditStatusEnum.AUDITING,
                action=MomentAuditLogActionEnum.CREATE,
                client_ip=client_ip,
                user_agent=user_agent,
            )
        )
        await session.commit()
        await session.refresh(dyn)
        logger.info(f"用户 {mid} 发布 WORD 动态 dynId={moment_id}（topics={len(topics) if topics else 0}）")
        return _to_base_resp(dyn)

    @staticmethod
    async def _create_forward(
        session: AsyncSession,
        mid: int,
        req: MomentCreateReq,
        nodes: list[MomentContentNode],
        attach: MomentAttachRef | None = None,
        topics: list[MomentTopicRef] | None = None,
        *,
        client_ip: str | None = None,
        user_agent: str | None = None,
    ) -> dict[str, Any]:
        if not req.repostSrc or req.repostSrc.dynId <= 0:
            raise ValueError("转发动态必须指定 repostSrc.dynId")
        src_dyn = await _get_dynamic_or_404(session, req.repostSrc.dynId)
        # 源动态必须已通过审核（normal）且未软删
        if src_dyn.auditStatus != MomentAuditStatusEnum.NORMAL or src_dyn.deletedAt is not None:
            raise ValueError("只能转发审核通过的动态")

        moment_id = await generate_moment_id()
        now = datetime.now()
        content_text = _nodes_to_text(nodes)
        # 2.22.0：多话题——主话题写 TMoment.topicId（=topics[0]），全部写 TMomentTopicRel
        topic_id = topics[0].topicId if topics else None
        option = req.option or None

        dyn = TMoment(
            dynId=moment_id,
            mid=mid,
            dynType=MomentTypeEnum.FORWARD,
            contentText=content_text,
            contentJson=[n.model_dump() for n in nodes],
            # 2.21.0：attach 卡只落 bizType+bizId（复用预留 bizType/bizRid 列；
            # bizType 为对外文字，落库前转 IntEnum）
            bizType=InteractionBizTypeEnum.from_text(attach.bizType) if attach and attach.bizType else None,
            bizRid=int(attach.bizId) if attach and attach.bizId else None,
            topicId=topic_id,
            repostSrcDynId=src_dyn.dynId,
            repostDepth=(src_dyn.repostDepth or 0) + 1,
            closeComment=option.closeComment if option else 0,
            # 2.46.0：FORWARD 一律强制 PUBLIC（服务端忽略传入值）
            visibleScope=MomentVisibleScopeEnum.PUBLIC,
            auditStatus=MomentAuditStatusEnum.AUDITING,
            created_at=now,
            updated_at=now,
        )
        session.add(dyn)
        await session.flush()
        if topics:
            await _persist_topic_rels(session, moment_id, [t.topicId for t in topics])
        _persist_resource_feed(session, moment_id, mid, topics, visible_scope=MomentVisibleScopeEnum.PUBLIC)
        # 注意：创建转发动态时源动态 repostCount 不 +1（状态机触发点 ⑥），
        # 必须等管理员审核通过（P6-T2）才对 srcDyn.repostCount +1。
        session.add(
            _build_audit_log(
                biz_type=InteractionBizTypeEnum.DYNAMIC,
                biz_id=moment_id,
                operator_mid=mid,
                to_status=MomentAuditStatusEnum.AUDITING,
                action=MomentAuditLogActionEnum.CREATE,
                from_status=MomentAuditStatusEnum.NORMAL,
                remark=f"repost from {src_dyn.dynId}",
                client_ip=client_ip,
                user_agent=user_agent,
            )
        )
        await session.commit()
        await session.refresh(dyn)
        logger.info(f"用户 {mid} 转发动态 srcDynId={src_dyn.dynId} → dynId={moment_id}（topics={len(topics) if topics else 0}）")
        return _to_base_resp(dyn)

    # ==================== 转发：repost 接口（P2-T3）====================

    @staticmethod
    async def repost(
        session: AsyncSession,
        mid: int,
        req: MomentRepostReq,
        *,
        client_ip: str | None = None,
        user_agent: str | None = None,
    ) -> dict[str, Any]:
        """转发指定动态（FORWARD）。与 create(FORWARD) 共用核心逻辑。"""
        src_dyn = await _get_dynamic_or_404(session, req.srcDynId)
        if src_dyn.auditStatus != MomentAuditStatusEnum.NORMAL or src_dyn.deletedAt is not None:
            raise ValueError("只能转发审核通过的动态")

        moment_id = await generate_moment_id()
        now = datetime.now()
        nodes = req.content or []
        content_text = _nodes_to_text(nodes)

        dyn = TMoment(
            dynId=moment_id,
            mid=mid,
            dynType=MomentTypeEnum.FORWARD,
            contentText=content_text,
            contentJson=[n.model_dump() for n in nodes],
            repostSrcDynId=src_dyn.dynId,
            repostDepth=(src_dyn.repostDepth or 0) + 1,
            # 2.46.0：FORWARD 一律强制 PUBLIC
            visibleScope=MomentVisibleScopeEnum.PUBLIC,
            auditStatus=MomentAuditStatusEnum.AUDITING,
            created_at=now,
            updated_at=now,
        )
        session.add(dyn)
        await session.flush()
        _persist_resource_feed(session, moment_id, mid, None, visible_scope=MomentVisibleScopeEnum.PUBLIC)
        session.add(
            _build_audit_log(
                biz_type=InteractionBizTypeEnum.DYNAMIC,
                biz_id=moment_id,
                operator_mid=mid,
                to_status=MomentAuditStatusEnum.AUDITING,
                action=MomentAuditLogActionEnum.CREATE,
                from_status=MomentAuditStatusEnum.NORMAL,
                remark=f"repost from {src_dyn.dynId}",
                client_ip=client_ip,
                user_agent=user_agent,
            )
        )
        await session.commit()
        await session.refresh(dyn)
        logger.info(f"用户 {mid} 转发动态(repost) srcDynId={src_dyn.dynId} → dynId={moment_id}")
        return _to_base_resp(dyn)

    # ==================== 编辑（P2-T4）====================

    @staticmethod
    async def edit(
        session: AsyncSession,
        mid: int,
        req: MomentEditReq,
        *,
        client_ip: str | None = None,
        user_agent: str | None = None,
    ) -> dict[str, Any]:
        """编辑动态。

        - rejected / auditing 编辑后自动回 auditing 重新审核；
        - normal 状态编辑 → 按状态机触发点 ③（离开 counting）→ 源动态 repostCount -1，
          本动态回到 auditing。
        """
        dyn_type = _precheck_scene(req.scene)
        attach, nodes = _resolve_attach(req)
        _precheck_content(nodes)
        await _validate_attach(session, attach)
        # 2.22.0：多话题（合并 topics+topic 去重，数量≤5，逐一校验 normal）
        topics = await _validate_topics(session, _resolve_topics(req))

        dyn = await _get_dynamic_or_404(session, req.dynId)
        if dyn.mid != mid:
            raise ValueError("只能编辑自己的动态")
        if dyn.deletedAt is not None:
            raise ValueError("动态已删除，无法编辑")

        was_normal = dyn.auditStatus == MomentAuditStatusEnum.NORMAL
        from_status = dyn.auditStatus

        # 状态机触发点 ③：编辑 normal 转发动态 → 源动态 repostCount -1
        if was_normal and dyn.dynType is MomentTypeEnum.FORWARD and dyn.repostSrcDynId:
            await MomentPublishService._decr_src_repost_count(session, dyn.repostSrcDynId)

        now = datetime.now()
        dyn.dynType = dyn_type
        dyn.contentText = _nodes_to_text(nodes)
        dyn.contentJson = [n.model_dump() for n in nodes]
        # 2.21.0：attach 卡随编辑更新 bizType/bizRid
        dyn.bizType = attach.bizType if attach else None
        dyn.bizRid = int(attach.bizId) if attach and attach.bizId else None
        # 2.22.0：多话题——主话题写 TMoment.topicId，关系表先删后插重建
        dyn.topicId = topics[0].topicId if topics else None
        await session.exec(delete(TMomentTopicRel).where(col(TMomentTopicRel.dynId) == dyn.dynId))
        if topics:
            await _persist_topic_rels(session, dyn.dynId, [t.topicId for t in topics])
        dyn.closeComment = req.option.closeComment if req.option else dyn.closeComment
        # 2.46.0：可见范围——FORWARD 恒 PUBLIC（服务端忽略传入值），WORD 可编辑时修改
        if dyn.dynType is MomentTypeEnum.FORWARD:
            dyn.visibleScope = MomentVisibleScopeEnum.PUBLIC
        elif req.option and req.option.visibleScope is not None:
            dyn.visibleScope = req.option.visibleScope
        # 回到审核中：清空驳回原因、pubTime，isTop 取消
        dyn.auditStatus = MomentAuditStatusEnum.AUDITING
        dyn.auditRejectReason = None
        dyn.pubTime = None
        dyn.isTop = 0
        dyn.topTime = None
        dyn.updated_at = now
        # 2.36.0：同步通用 Feed 元数据（回审核 + pubTime 置空 + 更新话题 tags）
        await session.exec(
            update(TResourceFeed)
            .where(
                col(TResourceFeed.bizType) == InteractionBizTypeEnum.DYNAMIC,
                col(TResourceFeed.bizId) == dyn.dynId,
            )
            .values(
                auditStatus="auditing",
                pubTime=None,
                tags=[t.topicId for t in topics] if topics else [],
                visibleScope=dyn.visibleScope,
            )
        )

        session.add(
            _build_audit_log(
                biz_type=InteractionBizTypeEnum.DYNAMIC,
                biz_id=dyn.dynId,
                operator_mid=mid,
                to_status=MomentAuditStatusEnum.AUDITING,
                action=MomentAuditLogActionEnum.EDIT,
                from_status=from_status,
                client_ip=client_ip,
                user_agent=user_agent,
            )
        )
        await session.commit()
        await session.refresh(dyn)
        logger.info(f"用户 {mid} 编辑动态 dynId={dyn.dynId}（before={from_status.value}）")
        return _to_base_resp(dyn)

    # ==================== 删除（P2-T4）====================

    @staticmethod
    async def remove(
        session: AsyncSession,
        mid: int,
        req: MomentRemoveReq,
        *,
        operator_role: str = "owner",
        client_ip: str | None = None,
        user_agent: str | None = None,
    ) -> dict[str, Any]:
        """软删动态（设 deletedAt）。

        ``operator_role``（2.22.1）：
        - ``"owner"``（默认）：仅作者本人可删（``dyn.mid != mid`` 抛 ValueError → 400）；
        - ``"admin"``：管理员删除，跳过作者校验，可删除**任意**动态。

        状态机触发点 ④：被删的 FORWARD 动态 before=normal → 源动态 repostCount -1。
        幂等：已删除（deletedAt 非空）直接返回成功。
        """
        dyn = await _get_dynamic_or_404(session, req.dynId)
        is_admin = operator_role == "admin"
        if not is_admin and dyn.mid != mid:
            raise ValueError("只能删除自己的动态")
        if dyn.deletedAt is not None:
            # 幂等：已删除直接返回成功
            return {"dynId": dyn.dynId, "dynIdStr": str(dyn.dynId), "success": True}

        was_normal = dyn.auditStatus == MomentAuditStatusEnum.NORMAL
        from_status = dyn.auditStatus

        # 状态机触发点 ④：软删 normal 转发动态 → 源动态 repostCount -1
        if was_normal and dyn.dynType is MomentTypeEnum.FORWARD and dyn.repostSrcDynId:
            await MomentPublishService._decr_src_repost_count(session, dyn.repostSrcDynId)

        now = datetime.now()
        dyn.deletedAt = now
        dyn.isTop = 0
        dyn.topTime = None
        dyn.updated_at = now
        # 2.36.0：同步通用 Feed 元数据（软删不入 Feed）
        await session.exec(
            update(TResourceFeed)
            .where(
                col(TResourceFeed.bizType) == InteractionBizTypeEnum.DYNAMIC,
                col(TResourceFeed.bizId) == dyn.dynId,
            )
            .values(deletedAt=now)
        )

        session.add(
            _build_audit_log(
                biz_type=InteractionBizTypeEnum.DYNAMIC,
                biz_id=dyn.dynId,
                operator_mid=mid,
                to_status=from_status,  # 软删不改 auditStatus，仅标记 deletedAt
                action=MomentAuditLogActionEnum.DELETE,
                from_status=from_status,
                remark="管理员删除" if is_admin else None,
                operator_role=(
                    MomentAuditLogOperatorRoleEnum.ADMIN if is_admin else MomentAuditLogOperatorRoleEnum.AUTHOR
                ),
                client_ip=client_ip,
                user_agent=user_agent,
            )
        )
        await session.commit()
        who = "管理员" if is_admin else f"用户 {mid}"
        logger.info(f"{who} 删除动态 dynId={dyn.dynId}（before={from_status.value}）")
        return {"dynId": dyn.dynId, "dynIdStr": str(dyn.dynId), "success": True}

    # ==================== 置顶 / 取消置顶（P2-T5）====================

    @staticmethod
    async def top(
        session: AsyncSession,
        mid: int,
        req: MomentTopReq,
        *,
        untop: bool = False,
    ) -> dict[str, Any]:
        """空间置顶 / 取消置顶。仅本人 + normal 状态可操作。"""
        dyn = await _get_dynamic_or_404(session, req.dynId)
        if dyn.mid != mid:
            raise ValueError("只能置顶自己的动态")
        if dyn.deletedAt is not None:
            raise ValueError("动态已删除，无法置顶")
        if dyn.auditStatus != MomentAuditStatusEnum.NORMAL:
            raise ValueError("仅审核通过的动态可置顶")
        if dyn.isTop == (0 if untop else 1):
            # 已是目标状态，幂等返回
            return {"dynId": dyn.dynId, "dynIdStr": str(dyn.dynId), "isTop": 0 if untop else 1}

        now = datetime.now()
        dyn.isTop = 0 if untop else 1
        dyn.topTime = None if untop else now
        dyn.updated_at = now
        await session.commit()
        await session.refresh(dyn)
        action = "取消置顶" if untop else "置顶"
        logger.info(f"用户 {mid} {action}动态 dynId={dyn.dynId}")
        return {"dynId": dyn.dynId, "dynIdStr": str(dyn.dynId), "isTop": dyn.isTop}

    # ==================== 发布时 @ 事件通知（P6-T8，弱依赖）====================

    @staticmethod
    async def _notify_at_batch(
        actor_mid: int, moment_id: int, nodes: list[MomentContentNode] | None
    ) -> None:
        """弱依赖：发布时解析正文中的 @ 节点，逐一对被 @ 用户发 AT 事件（DYNAMIC 来源）。

        独立会话投递：即便事件落库失败，也绝不回滚发布主事务。
        """
        from app.services.message.insite.events import report_event_weakly

        targets: set[int] = set()
        for n in nodes or []:
            if n.type != "AT" or not n.bizId:
                continue
            try:
                tmid = int(n.bizId)
            except (TypeError, ValueError):
                continue
            if tmid and tmid != actor_mid:
                targets.add(tmid)
        if not targets:
            return

        briefs = await PptrUser.get_many([actor_mid])
        actor_name = (
            briefs.get(actor_mid).uname if briefs.get(actor_mid) else None
        )
        for tmid in targets:
            await report_event_weakly(
                EventReportReq(
                    mid=tmid,
                    event_type=InteractionActionTypeEnum.AT,
                    source_type=InteractionBizTypeEnum.DYNAMIC,
                    source_id=str(moment_id),
                    actor_mid=actor_mid,
                    biz_id=str(moment_id),
                )
            )

    # ==================== 发布预校验（P2-T6 出参）====================

    @staticmethod
    async def create_check(mid: int, scene: str) -> dict[str, Any]:
        """发布页预校验：返回允许的场景与（预留的）设置 / 权限信息。"""
        allowed = list(_ALLOWED_SCENES)
        if scene not in _ALLOWED_SCENES:
            raise ValueError(f"暂不支持动态类型 {scene}")
        return {
            "setting": {},
            "permission": {"mid": mid},
            "allowedScenes": allowed,
        }

    # ==================== 内部：源动态 repostCount 原子 -1 ====================

    @staticmethod
    async def _decr_src_repost_count(session: AsyncSession, src_moment_id: int) -> None:
        """对源动态 repostCount 原子 -1（委托 MomentStatService，带 >0 兜底）。"""
        from app.services.moment.moment_stat import MomentStatService

        await MomentStatService.incr_repost_count(session, src_moment_id, -1)


__all__ = [
    "_ALLOWED_SCENES",
    "_AT_MAX_COUNT",
    "_CONTENT_MAX_LENGTH",
    "_MAX_TOPIC_COUNT",
    "MomentPublishService",
]
