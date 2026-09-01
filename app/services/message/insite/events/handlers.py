"""各类互动操作类型的事件处理器（msgfeed 内容体构建差异化）。

写路径（上报）与跨类型的读路径（聚合 / 明细 / msgfeed / 已读 / 计数）的公共逻辑
已下沉到 ``BaseEvent``；本模块只放「按类型差异化」的 msgfeed 内容体构建逻辑，
并在末尾把「类型 → 处理器」映射填入 ``registry.EVENT_REGISTRY``。
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from app.models.biz_type import source_type_to_biz_type
from app.models.enums import CommentStateEnum, InteractionActionTypeEnum
from app.models.schemas import EventMsgfeedContent

from .base import BaseEvent, GenericEvent
from .registry import EVENT_REGISTRY, EventSpec
from .source_meta import _resolve_source_meta

if TYPE_CHECKING:
    from app.models.db import EventMessage
    from .base import MsgfeedBuildContext


class LikeEvent(GenericEvent):
    """点赞事件。"""

    event_type = InteractionActionTypeEnum.LIKE
    # 闸门字段：用户消息设置里的 recv_like；系统通知为 None（恒投递）
    setting_gate = "recv_like"


class AtEvent(GenericEvent):
    """@提及事件。"""

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


class ReplyEvent(BaseEvent):
    """回复事件：msgfeed 需要回捞评论层级关系 / 正文 / 删除态。"""

    event_type = InteractionActionTypeEnum.REPLY
    # @ / 回复是「点对点打扰」，触发者与接收方存在黑名单关系时静默
    blocked_silent = True
    setting_gate = "recv_reply"

    async def build_msgfeed_content(
        self,
        ctx: "MsgfeedBuildContext",
        latest: "EventMessage",
        rows: list["EventMessage"],
    ) -> EventMsgfeedContent:
        biz_id = latest.biz_id or ""
        stype = latest.source_type
        idx = (
            ctx.comment_index.get(int(biz_id))
            if biz_id.isdigit()
            else None
        )

        biz_type = source_type_to_biz_type(stype)
        if biz_type is None:
            raise ValueError(
                f"事件来源类型 {stype} 无对应的业务资源类型，无法构建 msgfeed"
            )
        resource_type = biz_type.value
        resource_id = ""
        root_id = ""
        source_id = biz_id  # 当前评论 / 消息自身唯一 ID
        target_id = ""
        source_content = ""
        target_content = ""
        # 触发评论是否处于「非正常状态」（被删 / 未过审 / 驳回 / 下架 / 待审）：
        # 为 True 时正文不回捞，前端展示「该评论已被删除」占位（对齐知乎）。
        comment_deleted = False
        if idx is not None:
            if idx.state is not CommentStateEnum.NORMAL:
                comment_deleted = True
                resource_id = str(idx.oid)
            else:
                root_pk = idx.root or 0
                parent_pk = idx.parent or 0
                target_pk = parent_pk if parent_pk else root_pk
                resource_id = str(idx.oid)
                # 一级评论（root == 自身）时 root_id 等于 source_id
                root_id = str(root_pk) if root_pk else source_id
                # 直接回复目标；一级评论无目标则为空串
                target_id = str(target_pk) if target_pk else ""
                source_content = ctx.comment_content.get(int(biz_id), "")
                # target 对应评论正文（楼中楼 / 根评论均展示，用于通知上下文）
                if target_id and target_id.isdigit():
                    target_content = ctx.comment_content.get(int(target_id), "")
        else:
            # 非评论回复场景（点赞 / @ / 动态类）：resource_id 取 biz_id（资源 / 实体 id）
            resource_id = biz_id if biz_id.isdigit() else ""
            # 回复类事件拿不到 CommentIndex（评论被物理删除 / 历史行 biz_id 写成动态 oid）：
            # 无法回捞正文，同样标记占位，不再依赖事件表 content 兜底。
            comment_deleted = True

        title, image = await _resolve_source_meta(
            ctx.session, stype, latest.source_id, latest.biz_id, ctx.dyn_cache
        )

        return EventMsgfeedContent(
            item_id=latest.id or 0,
            type=int(self.event_type),
            business=stype.value if stype else 0,
            resource_type=resource_type,
            resource_id=resource_id,
            root_id=root_id,
            source_id=source_id,
            target_id=target_id,
            title=title,
            desc=latest.content or "",
            image=image,
            source_content=source_content,
            target_content=target_content,
            comment_deleted=comment_deleted,
            ctime=int(latest.created_at.timestamp()) if latest.created_at else 0,
        )


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
