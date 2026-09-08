"""审核结果 → 站内系统通知（给作者）。

当自动审核**未通过（REJECT）**（或进人工 AUDIT）时，给内容作者发一条站内系统通知，
告知未通过 / 进入审核的原因。正文里的敏感词**已打码**（复用 engine.reason 的脱敏文本，
不泄露完整敏感词）；命中词**原文不随通知下发**（原文仅供审核流水 / 数据库存储用）。

弱依赖：发通知失败不影响主流程（NotifyService.send_to_user 内部已捕获异常）。
"""

from __future__ import annotations

from app.models.enums import NotifyLevelEnum
from app.services.audit.engine import AuditResult
from app.services.message.insite.notify import NotifyService

# 未通过审核时的统一标题前缀（各资源拼接，如「动态」/「评论」）
DEFAULT_REJECT_TITLE = "内容未通过审核"


async def send_audit_notice(
    author_mid: int,
    result: AuditResult,
    *,
    subject_label: str = "内容",
    content_excerpt: str | None = None,
    jump_url: str | None = None,
    creator_mid: int = 0,
) -> None:
    """根据审核结果给作者发站内系统通知。

    Args:
        author_mid: 内容作者 mid（通知接收人）。
        result: 统一审核引擎的结果（decision / reason）。
        subject_label: 内容类型标签，如「动态」「评论」「话题」，用于标题与首句。
        content_excerpt: 被审内容简短摘录（可选，会一并告知但本身不应含敏感词）。
        jump_url: 站内跳转目标（前端路由串 / 外链，可选）。
        creator_mid: 通知发起方（0 = 系统）。
    """
    if result.passed:
        # 自动通过无需通知
        return

    reason = (result.reason or "").strip()
    if result.need_manual:
        title = f"{subject_label}进入人工审核"
        lines = [f"您的{subject_label}已提交，正在等待人工审核，请耐心等待。"]
        if content_excerpt:
            lines.append(f"内容：{content_excerpt}")
        if reason:
            lines.append(f"原因：{reason}")
    else:
        title = f"{subject_label}未通过审核"
        lines = [f"您的{subject_label}因命中平台审核规则，未能通过发布。"]
        if content_excerpt:
            lines.append(f"内容：{content_excerpt}")
        if reason:
            lines.append(f"命中：{reason}")

    await NotifyService.send_to_user(
        mid=author_mid,
        title=title,
        content="\n".join(lines),
        level=NotifyLevelEnum.IMPORTANT,
        jump_url=jump_url,
        creator_mid=creator_mid,
    )


__all__ = ["send_audit_notice", "DEFAULT_REJECT_TITLE"]
