"""互动操作类工厂（2.47.0）：按互动名 + `biz_type` 返回对应资源类型的操作类。

每个互动子类声明自己的 `_biz_type`（不可变，见 :class:`BaseInteractionAction`），
本工厂集中维护「互动操作 :class:`InteractionActionEnum` → {biz_type → 操作类}」映射，
供通用入口（HTTP 接口 / MQ 消费 / 其它服务）按资源类型分发实例化。

**盘点说明**：`:data:_ACTION_TABLES` 以 `InteractionActionEnum` 为 key 枚举了
通用内容型互动（点赞 / 点踩 / 收藏 / 分享 / **转发 / attach** / 浏览 / 举报）在
全部 6 个 `biz_type` 下的实现——非动态「转发」= attach 行为计数（复用
`repostCount`，见 `common/ResourceRepostAction`）；审核操作（`AUDIT_APPROVE` /
`AUDIT_REJECT`，DAC 审核员权限）当前仅动态已对象化，其余类型待接入对应审核服务后补全。
"""

from app.models.enums import InteractionBizTypeEnum
from app.services.interaction_actions.base import BaseInteractionAction, InteractionActionEnum
from app.services.interaction_actions.dynamic import (
    AuditApproveAction as DynamicAuditApproveAction,
    AuditRejectAction as DynamicAuditRejectAction,
    DislikeAction as DynamicDislikeAction,
    FavoriteAction as DynamicFavoriteAction,
    LikeAction as DynamicLikeAction,
    ReportAction as DynamicReportAction,
    RepostAction as DynamicRepostAction,
    ShareAction as DynamicShareAction,
    ViewAction as DynamicViewAction,
)
from app.services.interaction_actions.lottery import (
    DislikeAction as LotteryDislikeAction,
    FavoriteAction as LotteryFavoriteAction,
    LikeAction as LotteryLikeAction,
    ReportAction as LotteryReportAction,
    RepostAction as LotteryRepostAction,
    ShareAction as LotteryShareAction,
    ViewAction as LotteryViewAction,
)
from app.services.interaction_actions.rpa_action import (
    DislikeAction as RpaActionDislikeAction,
    FavoriteAction as RpaActionFavoriteAction,
    LikeAction as RpaActionLikeAction,
    ReportAction as RpaActionReportAction,
    RepostAction as RpaActionRepostAction,
    ShareAction as RpaActionShareAction,
    ViewAction as RpaActionViewAction,
)
from app.services.interaction_actions.rpa_browser import (
    DislikeAction as RpaBrowserDislikeAction,
    FavoriteAction as RpaBrowserFavoriteAction,
    LikeAction as RpaBrowserLikeAction,
    ReportAction as RpaBrowserReportAction,
    RepostAction as RpaBrowserRepostAction,
    ShareAction as RpaBrowserShareAction,
    ViewAction as RpaBrowserViewAction,
)
from app.services.interaction_actions.rpa_plugin import (
    DislikeAction as RpaPluginDislikeAction,
    FavoriteAction as RpaPluginFavoriteAction,
    LikeAction as RpaPluginLikeAction,
    ReportAction as RpaPluginReportAction,
    RepostAction as RpaPluginRepostAction,
    ShareAction as RpaPluginShareAction,
    ViewAction as RpaPluginViewAction,
)
from app.services.interaction_actions.rpa_workflow import (
    DislikeAction as RpaWorkflowDislikeAction,
    FavoriteAction as RpaWorkflowFavoriteAction,
    LikeAction as RpaWorkflowLikeAction,
    ReportAction as RpaWorkflowReportAction,
    RepostAction as RpaWorkflowRepostAction,
    ShareAction as RpaWorkflowShareAction,
    ViewAction as RpaWorkflowViewAction,
)


def _table(
    dynamic: type[BaseInteractionAction],
    lottery: type[BaseInteractionAction],
    rpa_action: type[BaseInteractionAction],
    rpa_workflow: type[BaseInteractionAction],
    rpa_browser: type[BaseInteractionAction],
    rpa_plugin: type[BaseInteractionAction],
) -> dict[InteractionBizTypeEnum, type[BaseInteractionAction]]:
    return {
        InteractionBizTypeEnum.DYNAMIC: dynamic,
        InteractionBizTypeEnum.LOTTERY: lottery,
        InteractionBizTypeEnum.RPA_ACTION: rpa_action,
        InteractionBizTypeEnum.RPA_WORKFLOW: rpa_workflow,
        InteractionBizTypeEnum.RPA_BROWSER: rpa_browser,
        InteractionBizTypeEnum.RPA_PLUGIN: rpa_plugin,
    }


#: 互动操作（InteractionActionEnum）→ {biz_type → 操作类}
#: - 通用内容型互动（点赞 / 点踩 / 收藏 / 分享 / 转发 / 浏览 / 举报）：6 个 biz_type 全支持
#:   （非动态「转发」= attach 行为计数，复用 repostCount）；
#: - 审核（通过 / 驳回）：DAC 审核员操作，当前仅动态已对象化（其余类型待接入对应审核服务）。
_ACTION_TABLES: dict[InteractionActionEnum, dict[InteractionBizTypeEnum, type[BaseInteractionAction]]] = {
    InteractionActionEnum.LIKE: _table(
        DynamicLikeAction,
        LotteryLikeAction,
        RpaActionLikeAction,
        RpaWorkflowLikeAction,
        RpaBrowserLikeAction,
        RpaPluginLikeAction,
    ),
    InteractionActionEnum.DISLIKE: _table(
        DynamicDislikeAction,
        LotteryDislikeAction,
        RpaActionDislikeAction,
        RpaWorkflowDislikeAction,
        RpaBrowserDislikeAction,
        RpaPluginDislikeAction,
    ),
    InteractionActionEnum.FAVORITE: _table(
        DynamicFavoriteAction,
        LotteryFavoriteAction,
        RpaActionFavoriteAction,
        RpaWorkflowFavoriteAction,
        RpaBrowserFavoriteAction,
        RpaPluginFavoriteAction,
    ),
    InteractionActionEnum.SHARE: _table(
        DynamicShareAction,
        LotteryShareAction,
        RpaActionShareAction,
        RpaWorkflowShareAction,
        RpaBrowserShareAction,
        RpaPluginShareAction,
    ),
    InteractionActionEnum.REPOST: _table(
        DynamicRepostAction,
        LotteryRepostAction,
        RpaActionRepostAction,
        RpaWorkflowRepostAction,
        RpaBrowserRepostAction,
        RpaPluginRepostAction,
    ),
    InteractionActionEnum.VIEW: _table(
        DynamicViewAction,
        LotteryViewAction,
        RpaActionViewAction,
        RpaWorkflowViewAction,
        RpaBrowserViewAction,
        RpaPluginViewAction,
    ),
    InteractionActionEnum.REPORT: _table(
        DynamicReportAction,
        LotteryReportAction,
        RpaActionReportAction,
        RpaWorkflowReportAction,
        RpaBrowserReportAction,
        RpaPluginReportAction,
    ),
    # 审核：仅动态已对象化（DAC 审核员操作；其余类型待接入对应审核服务后补全）
    InteractionActionEnum.AUDIT_APPROVE: {
        InteractionBizTypeEnum.DYNAMIC: DynamicAuditApproveAction,
    },
    InteractionActionEnum.AUDIT_REJECT: {
        InteractionBizTypeEnum.DYNAMIC: DynamicAuditRejectAction,
    },
}


def get_action(interaction, biz_type) -> type[BaseInteractionAction]:
    """按互动操作与资源类型返回对应的操作类。

    Args:
        interaction: 互动操作（`InteractionActionEnum` 成员，或文字 `like`/`dislike`/
            `favorite`/`share`/`view`/`report`…）。
        biz_type: 资源类型（dynamic / lottery / rpa_*，支持枚举 / 文字 / 数值）。

    Returns:
        对应资源类型的操作类（继承 :class:`BaseInteractionAction`）。

    Raises:
        ValueError: 不支持的互动操作 / 资源类型。
    """
    action = (
        interaction
        if isinstance(interaction, InteractionActionEnum)
        else InteractionActionEnum[interaction.upper()]
    )
    table = _ACTION_TABLES.get(action)
    if table is None:
        raise ValueError(f"不支持的互动操作: {action.name}")
    bt = InteractionBizTypeEnum.from_text(biz_type)
    action_cls = table.get(bt)
    if action_cls is None:
        raise ValueError(f"资源类型 {bt} 暂不支持互动操作 {action.name}")
    return action_cls


def get_like_action(biz_type) -> type[BaseInteractionAction]:
    """便捷封装：按资源类型返回点赞操作类。"""
    return get_action(InteractionActionEnum.LIKE, biz_type)


def get_favorite_action(biz_type) -> type[BaseInteractionAction]:
    """便捷封装：按资源类型返回收藏操作类。"""
    return get_action(InteractionActionEnum.FAVORITE, biz_type)


def get_view_action(biz_type) -> type[BaseInteractionAction]:
    """便捷封装：按资源类型返回浏览上报操作类。"""
    return get_action(InteractionActionEnum.VIEW, biz_type)


__all__ = [
    "InteractionActionEnum",
    "get_action",
    "get_like_action",
    "get_favorite_action",
    "get_view_action",
]
