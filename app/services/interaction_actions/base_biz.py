"""业务资源基类 `BaseBiz`（2.48.0）：**以 `InteractionBizTypeEnum` 为主体**。

设计要点
--------
1. **一个资源一个类**：每个 `InteractionBizTypeEnum` 成员对应一个继承 :class:`BaseBiz`
   的具体资源类（声明 `_biz_type`），在类里**实现**自己支持的操作；不再为每个
   「资源 × 操作」组合单独建类，也不再用工厂表登记（见第 3 点）。

2. **操作即接口**：`InteractionActionTypeEnum` 的全部成员都在本基类上声明为
   方法（**接口契约**）。基类默认实现抛 `NotImplementedError("该资源不支持…")`，
   资源类按需覆盖——这样接口完整可见，又不会强迫每个资源写一堆空实现。

3. **继承即登记（取代工厂注册）**：`__init_subclass__` 在子类声明 `_biz_type` 时
   自动写入 :data:`BaseBiz._registry`；新增资源只需「写类 + 声明 `_biz_type`」，
   无需再维护任何 `动作 → {资源 → 类}` 的映射表。

4. **权限仍是声明式的**：原「每动作一个类 + 类属性 `relation_scope` / `acl_scope`」
   的元数据改由 :func:`biz_action` 装饰器挂在方法上，执行前由基类集中强制校验，
   保留了模板方法的统一校验能力，避免在每个方法里手写校验而遗漏。

5. **举报体系合并进资源**：原 `BaseReportHandler` 的 `resolve_accused()` / `hide()`
   与举报管理操作（`report` / `report_reject` / `report_resolved`）统一成为
   本基类的方法，举报差异随资源走，协调器不再按 `bizType` 分流。
"""

import functools
import re
from abc import ABC
from typing import Any, Awaitable, Callable

from loguru import logger

from bili_common.models import InteractionActionTypeEnum, InteractionBizTypeEnum
from app.models.schemas.interaction import InteractionResource
from app.services.interaction_actions.base import (
    InteractionAclScopeEnum,
    InteractionActionError,
    InteractionRelationScopeEnum,
    _ACL_CHECKERS,
    _RELATION_CHECKERS,
)

__all__ = [
    "BaseBiz",
    "biz_action",
    "get_biz",
    "get_biz_class",
    "batch_get_resource_snapshots",
    "replace_at_mentions",
    "find_at_mention_mids",
]


# ==================== 声明式权限元数据装饰器 ====================

def biz_action(
    *,
    relation: list[InteractionRelationScopeEnum] | None = None,
    acl: list[InteractionAclScopeEnum] | None = None,
    require_resource: bool = True,
) -> Callable:
    """把一个操作方法标记为「受管控的业务操作」，并声明其权限要求。

    Args:
        relation: 关注关系权限检查项数组（空 = 无关系限制），如 ``[NOT_BLOCKED]``。
        acl: DAC 权限检查项数组（空 = 公开可操作），如 ``[AUDITOR_ONLY]``。
        require_resource: 是否先取资源并校验存在性 / 可互动（浏览等弱依赖操作设 False）。

    基类在调用真正实现前统一执行：取资源 → 存在性校验 → 关系权限 → DAC 权限。
    """

    def decorator(fn):
        @functools.wraps(fn)
        async def wrapper(self: "BaseBiz", *args, **kwargs):
            resource = None
            if require_resource:
                resource = await self.get_resource()
                await self.check_resource(resource)
            # 无论是否取资源，均执行声明式权限校验（acl 可仅依赖身份，无需资源对象）
            await self._enforce(resource, relation or [], acl or [])
            return await fn(self, *args, **kwargs)

        wrapper.__biz_action__ = True
        wrapper.__biz_scope__ = {
            "relation": list(relation or []),
            "acl": list(acl or []),
        }
        return wrapper

    return decorator


class BaseBiz(ABC):
    """业务资源基类（资源为主体，操作为方法）。

    子类必须声明 `_biz_type`；构造后以 ``self.session`` / ``self.biz_id`` /
    ``self.actor_mid`` 操作本资源实例，调用 ``await biz.like(...)`` 等方法。
    """

    #: 本类对应的资源类型（**不可变**，子类必须声明）
    _biz_type: InteractionBizTypeEnum | None = None
    #: 本资源举报记录的落库表（可举报资源声明；各表结构一致，继承 `ReportBase`）
    model: type | None = None

    # 继承即登记：子类声明 _biz_type 后自动进入本注册表（取代工厂映射表）
    _registry: dict[InteractionBizTypeEnum, type["BaseBiz"]] = {}

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        biz_type = getattr(cls, "_biz_type", None)
        if biz_type is not None:
            cls._registry[biz_type] = cls

    def __init__(self, session, biz_id: int, actor_mid: int | None = None) -> None:
        if self._biz_type is None:
            raise TypeError(
                f"{type(self).__name__} 必须声明 _biz_type（对应的 InteractionBizTypeEnum）"
            )
        self.session = session
        self.biz_id = int(biz_id)
        self.actor_mid = int(actor_mid) if actor_mid is not None else None
        # 客户端上下文（2.56.0 显式化）：发布 / 审核日志类操作需要，由接口层注入；
        # 默认 None，未注入时各资源方法按「无客户端信息」处理（取代原动态属性 + getattr 兜底）
        self.client_ip: str | None = None
        self.user_agent: str | None = None

    # ==================== 基本属性 ====================

    @property
    def biz_type(self) -> InteractionBizTypeEnum:
        """本资源类型（只读）。"""
        if self._biz_type is None:
            raise TypeError(
                f"{type(self).__name__} 必须声明 _biz_type（对应的 InteractionBizTypeEnum）"
            )
        return self._biz_type

    # ==================== 资源获取（统一装配 + 子类钩子）====================

    async def to_resource_model(self, rpid: str | None = None) -> InteractionResource:
        """转换本资源为统一的**资源信息模型** :class:`InteractionResource`。

        这是「任意 biz → 资源信息模型」的**唯一入口**：所有资源类（Dynamic / Comment /
        User / Lottery / RPA 系列）都经本方法产出结构一致的 :class:`InteractionResource`，
        供**互动提醒（通知）**与**举报审核管理**等读取侧复用同一套资源展示逻辑
        （标题 / 封面 / 作者 / 存在性 / 可互动性 / 后端跳转目标，计划书 §5.11 / C20）。

        组装逻辑统一调用子类钩子 ``check_exists()`` / ``_load_meta()`` /
        ``_load_author_mid()`` / ``_load_interactable()``，不在子类里散落存在性 /
        展示字段逻辑——子类只需覆盖各自钩子即自动获得完整转换能力。
        """
        exists = await self.check_exists()
        title: str | None = None
        cover: str | None = None
        author_mid: int | None = None
        interactable = exists
        if exists:
            title, cover = await self._load_meta()
            author_mid = await self._load_author_mid()
            inter = await self._load_interactable()
            interactable = inter if inter is not None else exists
        return InteractionResource(
            bizType=self.biz_type,
            bizId=self.biz_id,
            authorMid=author_mid,
            exists=exists,
            interactable=interactable,
            title=title,
            cover=cover,
            jumpTarget=self._build_jump_target(rpid),
        )

    async def get_resource(self, rpid: str | None = None) -> InteractionResource:
        """取本资源并折叠为统一 :class:`InteractionResource`（``to_resource_model`` 的兼容别名）。

        旧代码与互动操作入口（``biz_action`` 装饰器、``batch_get_resources``）仍调用本方法，
        行为等价于 :meth:`to_resource_model`。
        """
        return await self.to_resource_model(rpid)

    # ----- 子类钩子：统一由 get_resource 调用 -----

    async def check_exists(self) -> bool:
        """资源是否存在（默认 ``True``：无存在性概念的资源放行）。

        可互动 / 可举报资源应覆盖本方法（如动态查 ``TMoment``、抽奖查 RPC），
        把原 ``InteractionResourceValidator`` 注册式校验下沉为各资源类自身方法。
        """
        return True

    async def _load_meta(self) -> tuple[str | None, str | None]:
        """返回 ``(标题, 封面)``；默认 ``(None, None)``。资源覆盖以填充展示信息。"""
        return None, None

    async def _load_author_mid(self) -> int | None:
        """资源作者 mid；默认 ``None``。"""
        return None

    async def _load_interactable(self) -> bool | None:
        """可互动性（``None`` 表示沿用 ``exists``）。默认 ``None``。"""
        return None

    def _build_jump_target(self, rpid: str | None = None) -> str | None:
        """按资源类型拼出后端跳转目标（见 :func:`app.utils.route_target.jump_target_for`）。"""
        from app.utils.route_target import jump_target_for

        return jump_target_for(self.biz_type, self.biz_id, rpid)

    @classmethod
    async def batch_get_resources(
        cls,
        session,
        biz_ids: list[int],
        *,
        actor_mid: int | None = None,
        rpid_map: dict[int, str] | None = None,
    ) -> dict[int, InteractionResource]:
        """批量取资源快照（默认逐条 ``get_resource``；资源类可覆盖为一次 IN 查询 / 批量 RPC）。

        返回 ``biz_id(int) -> InteractionResource``；``rpid_map`` 携带楼层锚点（评论定位）。
        """
        out: dict[int, InteractionResource] = {}
        for bid in biz_ids:
            biz = cls(session, bid, actor_mid)
            out[bid] = await biz.to_resource_model(rpid=(rpid_map or {}).get(bid))
        return out

    async def check_resource(self, resource: InteractionResource) -> None:
        """校验资源是否存在 / 可互动（基类统一实现，子类可覆盖细化）。"""
        if not isinstance(resource, InteractionResource):
            raise TypeError(
                f"{type(self).__name__}.get_resource 必须返回 InteractionResource"
            )
        if not resource.exists:
            raise InteractionActionError(
                self.error_messages.get("not_found") or "资源不存在"
            )
        if not resource.interactable:
            raise InteractionActionError(
                self.error_messages.get("not_interactable")
                or self.error_messages.get("not_found")
                or "资源暂不可互动"
            )

    #: 报错信息（基类默认 + 子类覆盖 / 补充）
    error_messages: dict[str, str] = {
        "not_found": "资源不存在",
        "permission": "无权执行该操作",
        "blocked": "对方已将你加入黑名单，无法执行该操作",
        "invalid": "参数不合法",
        "acl_owner": "仅资源所有者可执行该操作",
        "acl_auditor": "仅审核员可执行该操作",
    }

    # ==================== 权限强制（基类统一，供装饰器调用）====================

    async def _enforce(
        self,
        resource: InteractionResource,
        relation: list[InteractionRelationScopeEnum],
        acl: list[InteractionAclScopeEnum],
    ) -> None:
        """按声明的 `relation` / `acl` 逐个执行校验（基类统一强制）。"""
        await self._check_relation(resource, relation)
        await self._check_acl(resource, acl)

    async def _check_relation(
        self, resource: InteractionResource, scopes: list[InteractionRelationScopeEnum]
    ) -> None:
        """按检查项数组校验关注 / 黑名单关系（资源无作者或操作者是本人时不拦截）。"""
        if not scopes:
            return
        author_mid = (
            resource.authorMid if resource.authorMid is not None else getattr(resource, "mid", None)
        )
        if author_mid is None or author_mid == self.actor_mid:
            return
        for scope in scopes:
            checker = _RELATION_CHECKERS.get(scope)
            if checker is None:
                logger.warning(f"未注册的关注关系权限检查项: {scope}，已跳过")
                continue
            await checker(self, int(author_mid))

    async def _check_acl(
        self, resource: InteractionResource, scopes: list[InteractionAclScopeEnum]
    ) -> None:
        """按检查项数组执行 DAC 权限校验（围绕资源展开）。"""
        if not scopes:
            return
        for scope in scopes:
            checker = _ACL_CHECKERS.get(scope)
            if checker is None:
                logger.warning(f"未注册的 DAC 权限检查项: {scope}，已跳过")
                continue
            await checker(self, resource)

    def _is_owner(self, resource: InteractionResource) -> bool:
        """资源所有者判断（DAC `OWNER_ONLY`）。"""
        owner = resource.ownerMid if resource.ownerMid is not None else resource.authorMid
        return owner is not None and owner == self.actor_mid

    def _is_auditor(self) -> bool:
        """审核员 / 管理员判断（DAC `AUDITOR_ONLY`）。默认 True——审核类接口在鉴权层
        （`RootUser` / `AdminUser`）已保证操作者为管理员，子类可覆盖为真实校验。"""
        return True

    # ==================== 操作接口（对应 InteractionActionTypeEnum 全成员）====================
    # 基类默认实现抛 NotImplementedError：接口完整可见，但只强制「语义上该有的」被实现。

    def _unsupported(self, action: InteractionActionTypeEnum) -> NotImplementedError:
        return NotImplementedError(
            f"{self.biz_type.to_text()} 资源不支持 {action.to_text()} 操作"
        )

    async def like(self, **kwargs):
        """点赞 / 取消点赞（`LIKE`）。"""
        raise self._unsupported(InteractionActionTypeEnum.LIKE)

    async def reply(self, **kwargs):
        """回复该资源（`REPLY`）。"""
        raise self._unsupported(InteractionActionTypeEnum.REPLY)

    async def at(self, **kwargs):
        """在该资源中 @ 提及用户（`AT`）。"""
        raise self._unsupported(InteractionActionTypeEnum.AT)

    async def audit_reject(self, **kwargs):
        """审核驳回（`AUDIT_REJECT`，DAC 审核员）。"""
        raise self._unsupported(InteractionActionTypeEnum.AUDIT_REJECT)

    async def hide(self, **kwargs):
        """隐藏 / 下架该资源（`HIDE`，管理端处置）。"""
        raise self._unsupported(InteractionActionTypeEnum.HIDE)

    async def report_reject(self, **kwargs):
        """举报审核驳回（`REPORT_REJECT`）：通知举报人「举报未通过」。"""
        raise self._unsupported(InteractionActionTypeEnum.REPORT_REJECT)

    async def report_resolved(self, **kwargs):
        """举报审核成立（`REPORT_RESOLVED`）：通知举报人「已成立」，可选下架资源。"""
        raise self._unsupported(InteractionActionTypeEnum.REPORT_RESOLVED)

    async def dislike(self, **kwargs):
        """点踩 / 取消点踩（`DISLIKE`）。"""
        raise self._unsupported(InteractionActionTypeEnum.DISLIKE)

    async def favorite(self, **kwargs):
        """收藏 / 取消收藏（`FAVORITE`）。"""
        raise self._unsupported(InteractionActionTypeEnum.FAVORITE)

    async def share(self, **kwargs):
        """分享上报（`SHARE`，不幂等）。"""
        raise self._unsupported(InteractionActionTypeEnum.SHARE)

    async def repost(self, **kwargs):
        """转发 / attach（`REPOST`）。"""
        raise self._unsupported(InteractionActionTypeEnum.REPOST)

    async def view(self, **kwargs):
        """浏览上报（`VIEW`，弱依赖计数）。"""
        raise self._unsupported(InteractionActionTypeEnum.VIEW)

    async def report(self, **kwargs):
        """举报该资源（`REPORT`）。"""
        raise self._unsupported(InteractionActionTypeEnum.REPORT)

    async def audit_approve(self, **kwargs):
        """审核通过（`AUDIT_APPROVE`，DAC 审核员）。"""
        raise self._unsupported(InteractionActionTypeEnum.AUDIT_APPROVE)

    # ==================== 举报相关（资源差异）====================

    async def resolve_accused(self) -> int:
        """定位被举报对象并取被举报用户 mid（不存在须抛 `ValueError`；
        弱依赖回查失败返回 0 = 未知作者）。可举报资源必须覆盖。"""
        raise NotImplementedError(
            f"{self.biz_type.to_text()} 资源未实现 resolve_accused（举报作者回查）"
        )

    # ==================== 举报审核（资源方法，通用实现）====================
    # report_reject / report_resolved 落到「本资源自身」的举报表（self.model），
    # 因此是全资源通用的——资源只需声明 model + resolve_accused + hide 即获得完整举报能力。

    async def _get_report_row(self, report_pk):
        """按主键取本资源举报表（self.model）中的记录。"""
        if self.model is None:
            raise NotImplementedError(f"{self.biz_type.to_text()} 资源不支持举报")
        from sqlmodel import select

        return (
            await self.session.exec(
                select(self.model).where(self.model.pk == report_pk)
            )
        ).one_or_none()

    async def _notify_audit_result(
        self,
        *,
        author_mid: int | None,
        passed: bool,
        reject_reason: str | None = None,
        remark: str | None = None,
    ) -> None:
        """审核结果通知作者（弱依赖，失败不阻塞）。

        与 `_notify_report_result`（通知举报人）成对：审核驳回**必须**通知作者，
        审核通过由各资源按策略决定是否调用（动态默认不通知）。
        """
        if not author_mid:
            return
        from app.models.schemas import EventReportReq
        from app.services.message.insite.events import report_event_weakly

        event_type = (
            InteractionActionTypeEnum.AUDIT_APPROVE
            if passed
            else InteractionActionTypeEnum.AUDIT_REJECT
        )
        content = (
            reject_reason
            or remark
            or ("你提交的内容已通过审核" if passed else "你提交的内容未通过审核")
        )
        await report_event_weakly(
            EventReportReq(
                mid=int(author_mid),
                event_type=event_type,
                source_type=self.biz_type,
                source_id=str(self.biz_id),
                actor_mid=self.actor_mid,
                content=content,
                biz_id=str(self.biz_id),
            )
        )

    async def _notify_report_result(self, rec, admin_mid: int, *, resolved: bool) -> None:
        """举报审核结果通知举报人（弱依赖，失败不阻塞）。"""
        from app.models.schemas import EventReportReq
        from app.services.message.insite.events import report_event_weakly

        event_type = (
            InteractionActionTypeEnum.REPORT_RESOLVED
            if resolved
            else InteractionActionTypeEnum.REPORT_REJECT
        )
        content = (
            "你提交的举报已成立并处理" if resolved else "你提交的举报未通过审核"
        )
        await report_event_weakly(
            EventReportReq(
                mid=rec.reportMid,
                event_type=event_type,
                source_type=self.biz_type,
                source_id=str(rec.bizId),
                actor_mid=admin_mid,
                content=content,
                biz_id=str(rec.bizId),
            )
        )

    @biz_action(acl=[InteractionAclScopeEnum.AUDITOR_ONLY], require_resource=False)
    async def report_reject(
        self, *, report_pk, admin_mid: int, remark: str | None = None
    ):
        """举报审核驳回：`REJECT` 置举报记录为 rejected，并通知举报人「未通过」。"""
        rec = await self._get_report_row(report_pk)
        if rec is None:
            raise ValueError("举报记录不存在")
        from bili_common.models.report import ReportReviewDecisionEnum
        from bili_common.services.report import ReportBaseService

        await ReportBaseService.review(
            session=self.session,
            model=self.model,
            report_pk=report_pk,
            admin_mid=admin_mid,
            decision=ReportReviewDecisionEnum.REJECT.value,
            remark=remark,
        )
        await self._notify_report_result(rec, admin_mid, resolved=False)
        return rec

    @biz_action(acl=[InteractionAclScopeEnum.AUDITOR_ONLY], require_resource=False)
    async def report_resolved(
        self,
        *,
        report_pk,
        admin_mid: int,
        remark: str | None = None,
        resource_action: str | None = None,
    ):
        """举报审核成立：`RESOLVE` 置举报记录为 resolved，通知举报人「已成立并处理」；
        可选 `resource_action="hide"` 联动下架本资源。"""
        rec = await self._get_report_row(report_pk)
        if rec is None:
            raise ValueError("举报记录不存在")
        from bili_common.models.report import ReportReviewDecisionEnum
        from bili_common.services.report import ReportBaseService

        await ReportBaseService.review(
            session=self.session,
            model=self.model,
            report_pk=report_pk,
            admin_mid=admin_mid,
            decision=ReportReviewDecisionEnum.RESOLVE.value,
            remark=remark,
        )
        await self._notify_report_result(rec, admin_mid, resolved=True)
        if resource_action == "hide":
            await self.hide(operator_mid=admin_mid)
        return rec


# ==================== 资源解析入口（取代工厂）====================

def get_biz_class(biz_type) -> type[BaseBiz]:
    """按资源类型返回对应的资源类（读继承登记表，无工厂映射表）。

    Args:
        biz_type: 资源类型（枚举 / 文字 / 数值）。

    Raises:
        ValueError: 该资源类型尚未实现资源类。
    """
    bt = InteractionBizTypeEnum.from_text(biz_type)
    biz_cls = BaseBiz._registry.get(bt)
    if biz_cls is None:
        raise ValueError(f"资源类型 {bt.to_text()} 尚未实现资源类")
    return biz_cls


def get_biz(biz_type, session, biz_id: int, actor_mid: int | None = None) -> BaseBiz:
    """按资源类型返回资源**实例**（资源为主体的统一入口）。

    用法：``biz = get_biz("dynamic", session, dyn_id, actor_mid=user.mid); await biz.like(up=1)``
    """
    biz_cls = get_biz_class(biz_type)
    return biz_cls(session, biz_id, actor_mid)


def registered_biz_types() -> list[InteractionBizTypeEnum]:
    """列出当前已实现资源类的全部资源类型（继承登记的盘点入口）。"""
    return list(BaseBiz._registry)


# ==================== 读取侧公共装配（事件提醒 / 举报审核等复用，计划书 §5.11/C20 / §5.12 / §5.19）====================
# 「任意 biz → 资源信息模型」的批量入口是 :meth:`BaseBiz.batch_get_resources`；
# 下列函数是**跨类型批量回捞**与**评论 @提及昵称替换**的公共封装——事件提醒
# （insite/events）与举报审核管理（admin/report）等读取侧不再各自维护一套同构代码，
# 统一引用本模块，避免逻辑漂移。均为**弱依赖**：失败降级为空占位 / 原样文本，不拖垮主链路。

_AT_MENTION_RE = re.compile(r"@\{(\d{1,19})\}")


def find_at_mention_mids(text: str | None) -> set[int]:
    """收集文本里所有 ``@{mid}`` 占位符的 mid（供调用方批量回查昵称，避免 N+1）。"""
    if not text:
        return set()
    return {int(m) for m in _AT_MENTION_RE.findall(text)}


def replace_at_mentions(text: str | None, nickname_map: dict[int, str]) -> str | None:
    """把正文里的 ``@{mid}`` 占位符替换为 ``@昵称``（公共：事件正文 / 举报评论标题复用）。

    - 只替换能命中 ``nickname_map``（mid → 昵称）的占位符；
    - 命中不到的（mid 不存在 / 用户已删 / 空昵称）原样保留，由前端兜底清理，
      避免把「@ 关系」丢成一个裸 ``@`` 或产生错误的人名。
    """
    if not text or not nickname_map:
        return text

    def _sub(match: re.Match[str]) -> str:
        mid = int(match.group(1))
        name = nickname_map.get(mid)
        if not name:
            return match.group(0)
        return f"@{name}"

    return _AT_MENTION_RE.sub(_sub, text)


async def batch_get_resource_snapshots(
    session, metas
) -> dict[tuple[int, str], InteractionResource]:
    """按 ``(resource_type, resource_id, rpid)`` 批量回捞资源快照（**每类资源一次调用**）。

    Args:
        session: 数据库会话（AsyncSession）。
        metas: 可迭代的 ``(resource_type, resource_id, rpid)``——``resource_type`` 为
            枚举 / 数值 / 文字（``InteractionBizTypeEnum``）；``resource_id`` 为 int 或
            数字字符串；``rpid`` 为楼层锚点（评论定位，仅评论锚定事件有意义，可为空）。

    Returns:
        ``{(resource_type:int, resource_id:str): InteractionResource}``。每类资源一次
        :meth:`BaseBiz.batch_get_resources`（动态 / 评论已是一次 IN 查询、RPA 系列走批量
        RPC，无 N+1）；未实现 / 未知资源类型 / 某类回查失败时，对应 id 返回
        ``exists=False`` 的空占位（读取侧展示「资源已删除 / 不存在」并跳过跳转）。
    """
    # 归一：resource_type / resource_id 统一为 (int, str)，并过滤空 id / 非法类型
    norm: list[tuple[int, str, str | None]] = []
    for rt, rid, rpid in metas:
        if not rid:
            continue
        try:
            rt_int = int(InteractionBizTypeEnum.from_text(rt))
        except (TypeError, ValueError):
            continue
        norm.append((rt_int, str(rid), rpid))

    # 分组（保留出现顺序，便于稳定回捞）
    by_type: dict[int, list[str]] = {}
    for rt_int, rid, _rpid in norm:
        by_type.setdefault(rt_int, []).append(rid)

    out: dict[tuple[int, str], InteractionResource] = {}
    for rt_int, ids in by_type.items():
        biz_cls = None
        try:
            biz_cls = get_biz_class(rt_int)
        except ValueError:  # 未知 / 未实现资源类型 → 整组空占位
            pass
        snaps: dict[int, InteractionResource] = {}
        if biz_cls is not None:
            rpid_map = {
                int(rid): rpid
                for t, rid, rpid in norm
                if t == rt_int and rid.isdigit() and rpid
            }
            try:
                snaps = await biz_cls.batch_get_resources(
                    session,
                    [int(i) for i in ids if i.isdigit()],
                    rpid_map=rpid_map,
                )
            except Exception as exc:  # 弱依赖：资源侧（含 RPC）不可用时降级为空占位
                logger.warning(
                    f"资源快照批量回捞失败（type={InteractionBizTypeEnum(rt_int).to_text()}），降级为空占位：{exc}"
                )
        for rid in ids:
            digit = rid.isdigit()
            snap = snaps.get(int(rid)) if digit else None
            out[(rt_int, rid)] = snap or InteractionResource(
                bizType=InteractionBizTypeEnum(rt_int),
                bizId=int(rid) if digit else 0,
                exists=False,
            )
    return out
