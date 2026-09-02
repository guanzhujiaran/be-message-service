"""各类互动操作类型的事件处理器（投递语义差异化）。

写路径（上报）与跨类型的读路径（聚合 / 明细 / msgfeed / 已读 / 计数）的公共逻辑
已下沉到 ``BaseEvent``；msgfeed 内容体构建也只有一份（``_generic_content``，
按 ``is_comment_anchored`` 决定走评论层级回捞还是直接取资源 id），
因此本模块**不再按类型重写内容体**，只声明各类型的投递语义
（``event_type`` / ``blocked_silent`` / ``setting_gate``），
并在末尾把「类型 → 处理器」映射填入 ``registry.EVENT_REGISTRY``。
"""
from __future__ import annotations

from bili_common.models import InteractionActionTypeEnum

from .base import GenericEvent
from .registry import EVENT_REGISTRY, EventSpec


class LikeEvent(GenericEvent):
    """点赞事件。

    评论点赞（``source_type=COMMENT``、``biz_id=rpid``）属评论锚定事件，
    内容体同样按评论层级回捞，可深链定位到被赞楼层。
    """

    event_type = InteractionActionTypeEnum.LIKE
    # 闸门字段：用户消息设置里的 recv_like；系统通知为 None（恒投递）
    setting_gate = "recv_like"


class AtEvent(GenericEvent):
    """@提及事件。

    评论里 @（``source_type=COMMENT``、``biz_id=rpid``）属评论锚定事件，
    内容体同样按评论层级回捞，可深链定位到被 @ 的那条评论。
    """

    event_type = InteractionActionTypeEnum.AT
    # @ / 回复是「点对点打扰」，触发者与接收方存在黑名单关系时静默
    blocked_silent = True
    setting_gate = "recv_at"


class AuditRejectEvent(GenericEvent):
    """内容审核驳回事件（通知作者）。"""

    event_type = InteractionActionTypeEnum.AUDIT_REJECT


class HideEvent(GenericEvent):
    """内容因举报被管理员下架事件（通知资源作者）。"""

    event_type = InteractionActionTypeEnum.HIDE


class ReportRejectEvent(GenericEvent):
    """举报未通过审核事件（通知举报人）。"""

    event_type = InteractionActionTypeEnum.REPORT_REJECT


class ReportResolvedEvent(GenericEvent):
    """举报成立已处理事件（通知举报人）。"""

    event_type = InteractionActionTypeEnum.REPORT_RESOLVED


class ReplyEvent(GenericEvent):
    """回复事件。

    ``biz_id`` 恒为评论 rpid，属「评论锚定」事件，内容体统一由
    ``GenericEvent.build_msgfeed_content`` → ``_locate_comment`` 回捞楼层关系 /
    正文 / 删除态，本类只声明投递语义。
    """

    event_type = InteractionActionTypeEnum.REPLY
    # @ / 回复是「点对点打扰」，触发者与接收方存在黑名单关系时静默
    blocked_silent = True
    setting_gate = "recv_reply"


# 类型 → 处理器 单一注册表（在处理器类定义完后填充）：
# 新增动作只需加一个处理器类（声明 event_type / blocked_silent / setting_gate），
# 再加一行 ``EVENT_REGISTRY[member] = EventSpec(member, HandlerClass)``，
# 黑名单静默与设置闸门由 handler 类属性自动生效。
EVENT_REGISTRY.update(
    {
        InteractionActionTypeEnum.LIKE: EventSpec(InteractionActionTypeEnum.LIKE, LikeEvent),
        InteractionActionTypeEnum.REPLY: EventSpec(InteractionActionTypeEnum.REPLY, ReplyEvent),
        InteractionActionTypeEnum.AT: EventSpec(InteractionActionTypeEnum.AT, AtEvent),
        InteractionActionTypeEnum.AUDIT_REJECT: EventSpec(
            InteractionActionTypeEnum.AUDIT_REJECT, AuditRejectEvent
        ),
        InteractionActionTypeEnum.HIDE: EventSpec(InteractionActionTypeEnum.HIDE, HideEvent),
        InteractionActionTypeEnum.REPORT_REJECT: EventSpec(
            InteractionActionTypeEnum.REPORT_REJECT, ReportRejectEvent
        ),
        InteractionActionTypeEnum.REPORT_RESOLVED: EventSpec(
            InteractionActionTypeEnum.REPORT_RESOLVED, ReportResolvedEvent
        ),
    }
)
