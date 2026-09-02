"""事件提醒对象模型（点赞 / 回复 / @提及 / 审核驳回 / 举报下架 / 举报结果）。

2.50.0 起从「静态方法工具类」重构为**面向对象**的事件处理器：

- 每种 ``InteractionActionTypeEnum`` 对应一个处理器类（``LikeEvent`` / ``ReplyEvent`` / ...），
  统一继承自虚基类 ``BaseEvent``；
- 写路径（上报）与跨类型的读路径（聚合 / 明细 / msgfeed / 已读 / 计数）的公共逻辑下沉到
  ``BaseEvent``；**msgfeed 内容体构建也只有一份**（``_generic_content`` 按
  ``is_comment_anchored`` 决定走评论层级回捞还是直接取资源 id），
  子类只声明投递语义（``blocked_silent`` / ``setting_gate``）；
- 业务方不再直接调 ``EventService``，而是 ``BaseEvent.from_req(req).report(session)``，
  或 ``ReplyEvent(mid=..., ...).report(session)`` 这样按对象操作，类型含义一目了然。

设计要点（沿用旧 ``EventService`` 的契约）：

- **上报（write）**：先过消息设置闸门 → 自赞过滤 → 计算幂等键 → 落库；
- **聚合读（read）**：按 ``source_type + source_id`` 分组，把「12 人赞了同一条动态」
  收敛成一张卡片；
- **已读管理**：支持按 id / 类型 / 聚合分组三种粒度。

幂等：``dedup_key``（唯一索引）由 ``mid:event_type:actor_mid:source_type:source_id:biz_id``
摘要而来。MQ 重投、前端重试、爬虫重复扫描都会被数据库直接拦掉。

模块拆分（本包）：

- ``constants``：常量与派生集合；
- ``source_meta``：动态正文首图提取（``_first_pic`` 共享工具，供 ``DynamicBiz`` 复用）；
- ``base``：``BaseEvent`` 虚基类 + ``MsgfeedBuildContext`` + 上报 / 读路径公共逻辑
  + 评论定位（``CommentLocate`` / ``is_comment_anchored``）；
- ``handlers``：各互动操作类型的处理器（投递语义差异化），并在末尾填充注册表；
- ``registry``：``EventSpec`` 规格与 ``EVENT_REGISTRY`` 空容器（单一真相源）。

保留与原 ``events.py`` 完全一致的公开 API，外部 ``from app.services.message.insite.events
import <Name>`` 无需改动。
"""
from .base import (
    BaseEvent,
    CommentLocate,
    GenericEvent,
    MsgfeedBuildContext,
    _resolve_handler_cls,
    build_dedup_key,
    is_comment_anchored,
    report_event_weakly,
)
from .handlers import (
    AtEvent,
    AuditRejectEvent,
    HideEvent,
    LikeEvent,
    ReplyEvent,
    ReportRejectEvent,
    ReportResolvedEvent,
)
from .registry import (
    EVENT_REGISTRY,
    EventSpec,
)

__all__ = [
    "EVENT_REGISTRY",
    "AtEvent",
    "AuditRejectEvent",
    "BaseEvent",
    "CommentLocate",
    "EventSpec",
    "GenericEvent",
    "HideEvent",
    "LikeEvent",
    "MsgfeedBuildContext",
    "ReplyEvent",
    "ReportRejectEvent",
    "ReportResolvedEvent",
    "build_dedup_key",
    "is_comment_anchored",
    "report_event_weakly",
]
