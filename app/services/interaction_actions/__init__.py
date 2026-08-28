"""互动操作对象模型（2.47.0）。

每个互动操作是一个继承 :class:`BaseInteractionAction` 的类，并**按 `biz_type` 分文件夹
组织**（每个类声明自己对应的 `_biz_type`，运行期经只读 property `biz_type` 读取，不可变）：

- 基类 / 权限注册表位于本包顶层 `base.py`（通用，不绑定具体资源）；
- `common/`：非动态资源复用的通用点赞 / 收藏实现；
- 各 `biz_type` 的互动子类放到对应文件夹：`dynamic/`（点赞 / 收藏 / 转发 / 分享 / 点踩）、
  `lottery/`、`rpa_action/`、`rpa_workflow/`、`rpa_browser/`、`rpa_plugin/`（点赞 / 收藏）；
- `factory.py`：按互动名 + `biz_type` 分发操作类的工厂（通用入口使用）。

接口层直接实例化对应操作类（从本包或对应子包导入），把各类 id 赋给初始化属性
（`biz_type` 无需传，类自带声明），调用 ``await XxxAction(...).run()`` 执行。

用法示例：

.. code-block:: python

    from app.services.interaction_actions import LikeAction, FavoriteAction
    from app.services.interaction_actions.factory import get_action

    is_like, count = await LikeAction(
        session, actor_mid=user.mid, biz_id=biz_id, up=req.up, dyn_id=req.dynId,
    ).run()  # LikeAction._biz_type = DYNAMIC
    favorited, folder_id = await FavoriteAction(
        session, actor_mid=user.mid, biz_id=biz_id, folder_id=req.folderId, action="add",
    ).run()
    # 通用入口按资源类型分发：
    action_cls = get_action("like", biz_type)   # lottery / rpa_* 也能取到对应类
    is_like, count = await action_cls(session, actor_mid=user.mid, biz_id=biz_id, up=1).run()

权限组合（子类类属性 ``relation_scope`` 为原子检查项数组，空列表 = 无限制）：

.. code-block:: python

    from app.services.interaction_actions.base import InteractionRelationScopeEnum

    class XxxAction(BaseInteractionAction):
        _biz_type = InteractionBizTypeEnum.DYNAMIC  # 每个类声明自己的资源类型
        # 组合：必须已关注作者 且 双向未被拉黑
        relation_scope = [
            InteractionRelationScopeEnum.FOLLOWING,
            InteractionRelationScopeEnum.NOT_BLOCKED,
        ]

新增权限（如会员专属 / 对方粉丝会员）：在 `InteractionRelationScopeEnum` 加枚举项，
并在 `base.py` 用 `@_relation_checker(枚举项)` 注册对应校验器即可，基类编排无需改动。
"""

from app.services.interaction_actions.base import (
    BaseInteractionAction,
    InteractionActionEnum,
    InteractionActionError,
    InteractionRelationScopeEnum,
)
from app.services.interaction_actions.dynamic import (
    DislikeAction,
    FavoriteAction,
    LikeAction,
    ReportAction,
    RepostAction,
    ShareAction,
    ViewAction,
)
from app.services.interaction_actions.factory import (
    get_action,
    get_favorite_action,
    get_like_action,
    get_view_action,
)

__all__ = [
    "BaseInteractionAction",
    "InteractionActionEnum",
    "InteractionActionError",
    "InteractionRelationScopeEnum",
    "LikeAction",
    "FavoriteAction",
    "RepostAction",
    "ShareAction",
    "DislikeAction",
    "ReportAction",
    "ViewAction",
    "get_action",
    "get_like_action",
    "get_favorite_action",
    "get_view_action",
]

