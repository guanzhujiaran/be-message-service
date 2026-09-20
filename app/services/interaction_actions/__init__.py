"""互动操作对象模型（2.48.0）：以 `InteractionBizTypeEnum` **资源为主体**。

每个资源一个继承 :class:`BaseBiz` 的类（`biz.py`），把
`InteractionActionTypeEnum` 的全部操作实现为方法；不再为「资源 × 操作」每个组合单独建类，
也不再用工厂表登记——资源类在**定义时**由 `__init_subclass__` 自动登记
（见 :mod:`app.services.interaction_actions.resources`）。

入口：`get_biz(biz_type, session, biz_id, actor_mid)` 取资源实例，直接调用
``await biz.like(up=1)`` / ``await biz.report(...)`` / ``await biz.report_resolved(...)`` 等方法。

用法示例：

.. code-block:: python

    from app.services.interaction_actions import get_biz, DynamicBiz

    biz = get_biz("dynamic", session, dyn_id, actor_mid=user.mid)
    is_like, count = await biz.like(up=1)
    # 或显式使用资源类
    created, triggered = await DynamicBiz(session, biz_id, actor_mid).report(reason_type=1)

权限组合（方法用 `@biz_action(relation=[...], acl=[...])` 声明，基类统一强制校验）：
见 :mod:`app.services.interaction_actions.base` 的 `InteractionRelationScopeEnum` /
`InteractionAclScopeEnum` 与对应校验器注册表。
"""

from bili_common.models import InteractionActionTypeEnum, InteractionBizTypeEnum
from app.services.interaction_actions.base import (
    InteractionAclScopeEnum,
    InteractionActionError,
    InteractionRelationScopeEnum,
)
from app.services.interaction_actions.base_biz import (
    BaseBiz,
    biz_action,
    get_biz,
    get_biz_class,
    registered_biz_types,
)
# 导入资源汇总模块：触发全部资源类的「继承即登记」副作用
from app.services.interaction_actions import resources  # noqa: F401
from app.services.interaction_actions.resources import (  # noqa: F401
    CommentBiz,
    DynamicBiz,
    LotteryBiz,
    OthersLotDynBiz,
    RpaActionBiz,
    RpaBrowserBiz,
    RpaPluginBiz,
    RpaTagBiz,
    RpaWorkflowBiz,
    UserBiz,
)

__all__ = [
    "BaseBiz",
    "biz_action",
    "get_biz",
    "get_biz_class",
    "registered_biz_types",
    "InteractionBizTypeEnum",
    "InteractionActionTypeEnum",
    "InteractionActionError",
    "InteractionRelationScopeEnum",
    "InteractionAclScopeEnum",
    "CommentBiz",
    "DynamicBiz",
    "LotteryBiz",
    "OthersLotDynBiz",
    "RpaActionBiz",
    "RpaBrowserBiz",
    "RpaPluginBiz",
    "RpaTagBiz",
    "RpaWorkflowBiz",
    "UserBiz",
]
