"""互动操作抽象基类（通用对象模型，2.47.0）。

把「点赞 / 收藏 / 转发 / 分享 / 点踩」等互动操作对象化：

- 每个互动操作是一个继承 :class:`BaseInteractionAction` 的类；
- 基类通过 ``relation_scope: list[InteractionRelationScopeEnum]``（原子权限检查项
  数组）统一控制每个操作对「关注 / 非关注 / 黑名单」等关系的权限（空列表 = 无限制，
  数组可任意组合，如 ``[NOT_BLOCKED, FOLLOWING]`` = 必须已关注且未被拉黑），
  并集中维护各操作的报错信息；
- 继承者实现四个接口（模板方法模式，由基类 ``run()`` 统一编排）：
  1. ``get_resource()``            —— 获取资源对象（不存在返回 None）；
  2. ``check_resource_exists()``   —— 检查资源是否存在，不存在抛 :class:`InteractionActionError`；
  3. ``do_execute()``              —— 执行互动操作（明细 + 计数同事务）；
  4. ``after_execute()``           —— 操作完成后的 hook（如系统消息通知，弱依赖不阻塞）。

接口层调用方式（不再依赖 moment 专属静态服务）：

.. code-block:: python

    action = LikeAction(session, actor_mid=user.mid, biz_id=biz_id,
                        up=req.up, dyn_id=req.dynId)   # biz_type 由类声明（不可变）
    is_like, count = await action.run()
"""

from abc import ABC, abstractmethod
from enum import IntEnum
from typing import Any, Awaitable, Callable

from loguru import logger
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.enums import InteractionBizTypeEnum
from app.models.schemas.interaction import InteractionResource
from app.services.user.follow import FollowService


class InteractionActionEnum(IntEnum):
    """**互动操作类型**枚举（对象模型的「动作名」全集）。

    作为 :data:`interaction_actions.factory._ACTION_TABLES` 的 key，统一盘点
    每个 `biz_type` 支持哪些互动操作、还缺哪些。新增互动操作时先在**此处声明
    枚举项**，再在 `factory` 注册对应表，即可被通用入口 `get_action` 分发。

    通用互动（内容型资源普遍适用）：点赞 / 点踩 / 收藏 / 分享 / 转发 / 浏览 / 举报 /
    审核通过 / 审核驳回。
    资源特有互动（仅特定 biz_type 语义成立）：关注 / 投币 / 置顶等按需声明。
    """

    LIKE = 1  # 点赞 / 取消点赞
    DISLIKE = 2  # 点踩 / 取消点踩（EdgeRank 降权）
    FAVORITE = 3  # 收藏 / 取消收藏（多夹 + 用户去重计数）
    SHARE = 4  # 分享上报（shareCount +1，行为上报不幂等）
    REPOST = 5  # 转发 / attach（动态=生成 FORWARD；非动态=attach 行为计数 repostCount +1）
    VIEW = 6  # 浏览上报（弱依赖计数，跨天去重）
    REPORT = 7  # 举报（不改 auditStatus）
    AUDIT_APPROVE = 8  # 审核通过（DAC：仅审核员）
    AUDIT_REJECT = 9  # 审核驳回（DAC：仅审核员）


class InteractionAclScopeEnum(IntEnum):
    """**DAC（自主访问控制）权限检查项**（围绕资源展开，可组合为数组）。

    与 :class:`InteractionRelationScopeEnum`（操作者 ↔ 资源作者的**关系**权限）互补：
    本枚举描述操作者**身份 / 授权**（谁能对资源做什么），空列表 = 公开可操作。

    - `OWNER_ONLY`：仅资源所有者可操作（编辑 / 删除等）；
    - `AUDITOR_ONLY`：仅审核员 / 管理员可操作（审核通过 / 驳回等）。

    后续新增权限（如 `VIP_ONLY` 会员专属、`SUPPORTER_ONLY` 对方粉丝会员等）只需：
    加枚举项 + 在 :data:`_ACL_CHECKERS` 注册对应校验器，即可被任意操作组合使用。
    """

    OWNER_ONLY = 1
    AUDITOR_ONLY = 2


class InteractionRelationScopeEnum(IntEnum):
    """互动操作的**原子**关注关系权限检查项（可组合为数组）。

    - `FOLLOWING`：操作者必须已关注作者；
    - `NON_FOLLOWING`：操作者必须未关注作者；
    - `NOT_BLOCKED`：黑名单禁止，双向任一向存在拉黑关系即拒绝。

    后续新增权限（如 `VIP_ONLY` 会员专属、`SUPPORTER` 对方粉丝会员等）只需：
    加枚举项 + 在 :data:`_RELATION_CHECKERS` 注册对应校验器，即可被任意操作组合使用。
    """

    FOLLOWING = 1
    NON_FOLLOWING = 2
    NOT_BLOCKED = 3


class InteractionActionError(ValueError):
    """互动操作业务错误（携带对外 msg / 语义 code，兼容既有 ``except ValueError``）。"""

    def __init__(self, msg: str, code: int = 400) -> None:
        super().__init__(msg)
        self.msg = msg
        self.code = code


# ==================== 关注关系权限检查项注册表 ====================
# 每个原子权限项对应一个独立校验器；新增权限 = 加枚举 + 在此注册校验函数，
# 基类 `_check_relation` 无需改动，即可被任意操作的 relation_scope 组合使用。

RelationScopeChecker = Callable[
    ["BaseInteractionAction", int], Awaitable[None]
]

_RELATION_CHECKERS: dict[InteractionRelationScopeEnum, RelationScopeChecker] = {}


def _relation_checker(scope: InteractionRelationScopeEnum) -> Callable[[RelationScopeChecker], RelationScopeChecker]:
    """注册「关注关系权限检查项 → 校验器」的装饰器。"""

    def decorator(fn: RelationScopeChecker) -> RelationScopeChecker:
        _RELATION_CHECKERS[scope] = fn
        return fn

    return decorator


@_relation_checker(InteractionRelationScopeEnum.FOLLOWING)
async def _check_scope_following(action: "BaseInteractionAction", author_mid: int) -> None:
    """仅关注者可操作：操作者未关注作者时拒绝。"""
    if not await FollowService.is_following(action.session, action.actor_mid, author_mid):
        raise InteractionActionError(
            action.error_messages.get("permission") or "仅关注后可以执行该操作"
        )


@_relation_checker(InteractionRelationScopeEnum.NON_FOLLOWING)
async def _check_scope_non_following(action: "BaseInteractionAction", author_mid: int) -> None:
    """仅未关注者可操作：操作者已关注作者时拒绝。"""
    if await FollowService.is_following(action.session, action.actor_mid, author_mid):
        raise InteractionActionError(
            action.error_messages.get("permission") or "该操作仅对未关注用户开放"
        )


@_relation_checker(InteractionRelationScopeEnum.NOT_BLOCKED)
async def _check_scope_not_blocked(action: "BaseInteractionAction", author_mid: int) -> None:
    """黑名单禁止：双向任一向存在拉黑关系即拒绝。"""
    if await FollowService.is_blocked_relation(action.session, action.actor_mid, author_mid):
        raise InteractionActionError(
            action.error_messages.get("blocked") or "对方已将你加入黑名单，无法执行该操作"
        )


# ==================== DAC 权限检查项注册表（围绕资源展开）====================
# 每个原子权限项对应一个独立校验器；新增权限 = 加枚举 + 在此注册校验函数，
# 基类 `_check_acl` 无需改动，即可被任意操作的 acl_scope 组合使用。

AclScopeChecker = Callable[
    ["BaseInteractionAction", "InteractionResource"], Awaitable[None]
]

_ACL_CHECKERS: dict[InteractionAclScopeEnum, AclScopeChecker] = {}


def _acl_checker(scope: InteractionAclScopeEnum) -> Callable[[AclScopeChecker], AclScopeChecker]:
    """注册「DAC 权限检查项 → 校验器」的装饰器。"""

    def decorator(fn: AclScopeChecker) -> AclScopeChecker:
        _ACL_CHECKERS[scope] = fn
        return fn

    return decorator


@_acl_checker(InteractionAclScopeEnum.OWNER_ONLY)
async def _check_acl_owner_only(action: "BaseInteractionAction", resource: "InteractionResource") -> None:
    """仅资源所有者可操作：操作者不是资源所有者（或资源无所有者）时拒绝。"""
    if not action._is_owner(resource):
        raise InteractionActionError(
            action.error_messages.get("acl_owner") or "仅资源所有者可执行该操作"
        )


@_acl_checker(InteractionAclScopeEnum.AUDITOR_ONLY)
async def _check_acl_auditor_only(action: "BaseInteractionAction", resource: "InteractionResource") -> None:
    """仅审核员 / 管理员可操作：操作者不是审核员时拒绝。"""
    if not action._is_auditor():
        raise InteractionActionError(
            action.error_messages.get("acl_auditor") or "仅审核员可执行该操作"
        )


class BaseInteractionAction(ABC):
    """互动操作抽象基类（模板方法模式，通用不绑定具体资源）。"""

    #: 该操作对应的资源类型（**不可变**：子类必须声明 `_biz_type`，
    #: 运行期经只读 property :attr:`biz_type` 读取；不同资源类型的互动
    #: 各自成类并声明自己的 `_biz_type`）
    _biz_type: InteractionBizTypeEnum | None = None

    #: 该操作默认的关注关系权限（原子检查项数组，空列表 = 无关系限制；子类可覆盖 / 组合）
    relation_scope: list[InteractionRelationScopeEnum] = []

    #: 该操作的 DAC 权限（原子检查项数组，围绕资源展开；空列表 = 公开可操作；子类可覆盖 / 组合）
    acl_scope: list[InteractionAclScopeEnum] = []

    #: 报错信息集中维护（基类默认 + 子类覆盖 / 按 key 补充）
    error_messages: dict[str, str] = {
        "not_found": "资源不存在",
        "permission": "无权执行该操作",
        "blocked": "对方已将你加入黑名单，无法执行该操作",
        "invalid": "参数不合法",
        "acl_owner": "仅资源所有者可执行该操作",
        "acl_auditor": "仅审核员可执行该操作",
    }

    def __init__(
        self,
        session: AsyncSession,
        actor_mid: int,
        biz_id: int,
        **ids: Any,
    ) -> None:
        """把各类 id 直接赋予实例属性。

        Args:
            session: 数据库会话（由调用方创建 / 提交）。
            actor_mid: 操作者 mid。
            biz_id: 资源 id（动态时=dynId）。
            **ids: 其余 id（dyn_id / folder_id / folder_id / up / action 等）直接挂为实例属性。
        """
        # 强制校验子类已声明对应的资源类型（基类本身不绑定具体 biz_type）
        if self.biz_type is None:
            raise TypeError(
                f"{type(self).__name__} 必须声明 _biz_type（对应的 InteractionBizTypeEnum）"
            )
        self.session = session
        self.actor_mid = int(actor_mid)
        self.biz_id = int(biz_id)
        for key, val in ids.items():
            setattr(self, key, val)

    @property
    def biz_type(self) -> InteractionBizTypeEnum:
        """本操作对应的资源类型（只读，不可变；由子类声明 `_biz_type`）。"""
        if self._biz_type is None:
            raise TypeError(
                f"{type(self).__name__} 必须声明 _biz_type（对应的 InteractionBizTypeEnum）"
            )
        return self._biz_type

    # ==================== 模板方法（调用方入口）====================

    async def run(self) -> Any:
        """执行完整互动流程：获取资源 → 存在性校验 → 关系权限校验 → DAC 权限校验 → 执行 → hook。"""
        resource = await self.get_resource()
        await self.check_resource_exists(resource)
        await self._check_relation(resource)
        await self._check_acl(resource)
        result = await self.do_execute(resource)
        await self.after_execute(resource, result)
        return result

    # ==================== 基类统一实现 ====================

    async def _check_acl(self, resource: InteractionResource) -> None:
        """按 ``acl_scope`` 数组逐个执行 DAC 权限校验（基类统一实现，围绕资源展开）。

        每个检查项对应 :data:`_ACL_CHECKERS` 中注册的独立校验器，
        新增权限只需注册新校验器，无需改动本方法。
        """
        if not self.acl_scope:
            return
        if not isinstance(resource, InteractionResource):
            raise TypeError(
                f"{type(self).__name__}.get_resource 必须返回 InteractionResource"
            )
        for scope in self.acl_scope:
            checker = _ACL_CHECKERS.get(scope)
            if checker is None:
                logger.warning(f"未注册的 DAC 权限检查项: {scope}，已跳过")
                continue
            await checker(self, resource)

    def _is_owner(self, resource: InteractionResource) -> bool:
        """资源所有者判断（DAC `OWNER_ONLY`）。

        默认比较 ``ownerMid``（缺失时回退 ``authorMid``，内容型资源作者即所有者）
        与 ``actor_mid``；资源无任何归属信息时视为非本人，拒绝授权。
        子类可覆盖（如经 RPC 查归属）。
        """
        owner = resource.ownerMid if resource.ownerMid is not None else resource.authorMid
        return owner is not None and owner == self.actor_mid

    def _is_auditor(self) -> bool:
        """审核员 / 管理员判断（DAC `AUDITOR_ONLY`）。

        默认 True——审核类接口在鉴权层（`RootUser`）已保证操作者为管理员；
        子类可在需要时覆盖为真实审核员校验。
        """
        return True

    async def _check_relation(self, resource: Any) -> None:
        """按 ``relation_scope`` 数组逐个校验关注 / 黑名单关系（基类统一实现）。

        资源无作者（非动态资源本地无作者信息）或操作对象是本人时不拦截。
        每个检查项对应 :data:`_RELATION_CHECKERS` 中注册的独立校验器，
        新增权限只需注册新校验器，无需改动本方法。
        """
        if not self.relation_scope:
            return
        author_mid = self._get_author_mid(resource)
        if author_mid is None or author_mid == self.actor_mid:
            return
        for scope in self.relation_scope:
            checker = _RELATION_CHECKERS.get(scope)
            if checker is None:
                logger.warning(f"未注册的关注关系权限检查项: {scope}，已跳过")
                continue
            await checker(self, author_mid)

    def _get_author_mid(self, resource: Any) -> int | None:
        """取资源作者 mid（统一 Resource 用 ``authorMid``；兼容旧形态资源对象取 ``.mid``）。"""
        if isinstance(resource, InteractionResource):
            return resource.authorMid
        if resource is None:
            return None
        return getattr(resource, "mid", None) or getattr(resource, "authorMid", None)

    # ==================== 继承者必须实现的接口 ====================

    @abstractmethod
    async def get_resource(self) -> InteractionResource:
        """获取资源并折叠为统一 :class:`InteractionResource`（不存在 / 不可互动时
        置 ``exists=False`` / ``interactable=False``，由基类 ``check_resource_exists`` 抛错）。"""

    async def check_resource_exists(self, resource: InteractionResource) -> None:
        """检查资源是否存在 / 可互动（基类统一实现；子类可覆盖细化）。

        基于 ``get_resource`` 返回的统一 :class:`InteractionResource`：
        - ``exists=False`` → 抛 `not_found`；
        - ``interactable=False`` → 抛 `not_interactable`（缺省回退 `not_found`）。
        """
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

    @abstractmethod
    async def do_execute(self, resource: InteractionResource) -> Any:
        """执行互动操作（明细 + 计数同事务），返回操作结果。"""

    async def after_execute(self, resource: InteractionResource, result: Any) -> None:
        """操作完成后的 hook（默认空实现；子类可覆盖，如系统消息通知，弱依赖不阻塞）。"""


__all__ = [
    "BaseInteractionAction",
    "InteractionActionError",
    "InteractionRelationScopeEnum",
]
