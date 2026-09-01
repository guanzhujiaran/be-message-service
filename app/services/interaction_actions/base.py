"""互动操作权限模型（2.48.0，通用、不绑定具体资源）。

本模块只承载**声明式权限原语**，被 :mod:`app.services.interaction_actions.base_biz`
的 :func:`biz_action` 装饰器在资源方法执行前统一调用：

- `InteractionRelationScopeEnum`：关注关系权限**原子检查项**（操作者 ↔ 资源作者）；
- `InteractionAclScopeEnum`：DAC 权限**原子检查项**（操作者身份 / 授权）；
- 两个注册表 `_RELATION_CHECKERS` / `_ACL_CHECKERS` + 校验器；
- `InteractionActionError`：业务错误（携带对外 msg / code）。

新增权限（如会员专属 / 对方粉丝会员）= 在枚举加项 + 在此用 `@_relation_checker` /
`@_acl_checker` 注册校验函数即可，资源方法的权限组合无需改动。
"""

from enum import IntEnum
from typing import Any, Awaitable, Callable

from loguru import logger

from app.models.enums import InteractionActionTypeEnum, InteractionBizTypeEnum
from app.models.schemas.interaction import InteractionResource
from app.services.user.follow import FollowService


class InteractionAclScopeEnum(IntEnum):
    """**DAC（自主访问控制）权限检查项**（围绕资源展开，可组合为数组）。

    与 :class:`InteractionRelationScopeEnum`（操作者 ↔ 资源作者的**关系**权限）互补：
    本枚举描述操作者**身份 / 授权**（谁能对资源做什么），空列表 = 公开可操作。

    - `OWNER_ONLY`：仅资源所有者可操作（编辑 / 删除等）；
    - `AUDITOR_ONLY`：仅审核员 / 管理员可操作（审核通过 / 驳回等）。
    """

    OWNER_ONLY = 1
    AUDITOR_ONLY = 2


class InteractionRelationScopeEnum(IntEnum):
    """互动操作的**原子**关注关系权限检查项（可组合为数组）。

    - `FOLLOWING`：操作者必须已关注作者；
    - `NON_FOLLOWING`：操作者必须未关注作者；
    - `NOT_BLOCKED`：黑名单禁止，双向任一向存在拉黑关系即拒绝。
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

RelationScopeChecker = Callable[["_BizLike", int], Awaitable[None]]

_RELATION_CHECKERS: dict[InteractionRelationScopeEnum, RelationScopeChecker] = {}


def _relation_checker(scope: InteractionRelationScopeEnum) -> Callable[[RelationScopeChecker], RelationScopeChecker]:
    """注册「关注关系权限检查项 → 校验器」的装饰器。"""

    def decorator(fn: RelationScopeChecker) -> RelationScopeChecker:
        _RELATION_CHECKERS[scope] = fn
        return fn

    return decorator


@_relation_checker(InteractionRelationScopeEnum.FOLLOWING)
async def _check_scope_following(action: "_BizLike", author_mid: int) -> None:
    """仅关注者可操作：操作者未关注作者时拒绝。"""
    if not await FollowService.is_following(action.session, action.actor_mid, author_mid):
        raise InteractionActionError(
            action.error_messages.get("permission") or "仅关注后可以执行该操作"
        )


@_relation_checker(InteractionRelationScopeEnum.NON_FOLLOWING)
async def _check_scope_non_following(action: "_BizLike", author_mid: int) -> None:
    """仅未关注者可操作：操作者已关注作者时拒绝。"""
    if await FollowService.is_following(action.session, action.actor_mid, author_mid):
        raise InteractionActionError(
            action.error_messages.get("permission") or "该操作仅对未关注用户开放"
        )


@_relation_checker(InteractionRelationScopeEnum.NOT_BLOCKED)
async def _check_scope_not_blocked(action: "_BizLike", author_mid: int) -> None:
    """黑名单禁止：双向任一向存在拉黑关系即拒绝。"""
    if await FollowService.is_blocked_relation(action.session, action.actor_mid, author_mid):
        raise InteractionActionError(
            action.error_messages.get("blocked") or "对方已将你加入黑名单，无法执行该操作"
        )


# ==================== DAC 权限检查项注册表（围绕资源展开）====================

AclScopeChecker = Callable[["_BizLike", "InteractionResource"], Awaitable[None]]

_ACL_CHECKERS: dict[InteractionAclScopeEnum, AclScopeChecker] = {}


def _acl_checker(scope: InteractionAclScopeEnum) -> Callable[[AclScopeChecker], AclScopeChecker]:
    """注册「DAC 权限检查项 → 校验器」的装饰器。"""

    def decorator(fn: AclScopeChecker) -> AclScopeChecker:
        _ACL_CHECKERS[scope] = fn
        return fn

    return decorator


@_acl_checker(InteractionAclScopeEnum.OWNER_ONLY)
async def _check_acl_owner_only(action: "_BizLike", resource: "InteractionResource") -> None:
    """仅资源所有者可操作：操作者不是资源所有者（或资源无所有者）时拒绝。"""
    if not action._is_owner(resource):
        raise InteractionActionError(
            action.error_messages.get("acl_owner") or "仅资源所有者可执行该操作"
        )


@_acl_checker(InteractionAclScopeEnum.AUDITOR_ONLY)
async def _check_acl_auditor_only(action: "_BizLike", resource: "InteractionResource") -> None:
    """仅审核员 / 管理员可操作：操作者不是审核员时拒绝。"""
    if not action._is_auditor():
        raise InteractionActionError(
            action.error_messages.get("acl_auditor") or "仅审核员可执行该操作"
        )


# 类型占位：兼容校验器签名中的 `action` 形态（BaseBiz 具备 session/actor_mid/error_messages/_is_owner/_is_auditor）
class _BizLike:
    session: Any
    actor_mid: int
    error_messages: dict[str, str]

    def _is_owner(self, resource: InteractionResource) -> bool: ...
    def _is_auditor(self) -> bool: ...


__all__ = [
    "InteractionActionError",
    "InteractionRelationScopeEnum",
    "InteractionAclScopeEnum",
]
