"""message-service 业务异常（继承 bili_common 统一异常体系）。

统一由 bili_common 的 `register_exception_handlers` 归一化为 {code, msg, data} 响应，
各服务模块直接抛出本模块定义的异常即可，勿在业务代码里再抛裸 ValueError。
"""

from bili_common.exceptions import BiliException
from bili_common.models.response_code import ResponseCode

from app.models.enums import ResourceAuditStatusEnum


class CommentNotInteractiveException(BiliException):
    """评论当前不可互动（不存在 / 已删除 / 审核中 / 已驳回 / 已下架）。

    评论存在但状态不可见时（如审核中、被驳回、下架），按具体状态给出准确反馈，
    避免一律误报为「不存在」。HTTP 404 + 业务码 404，供网关/前端按资源缺失语义归类。
    """

    # 评论生命周期状态 → 对外反馈（仅覆盖不可互动的非 NORMAL 状态）
    _STATE_MSG: dict[ResourceAuditStatusEnum, str] = {
        ResourceAuditStatusEnum.AUDITING: "评论审核中，暂不可互动",
        ResourceAuditStatusEnum.REJECTED: "评论未通过审核，不可互动",
        ResourceAuditStatusEnum.HIDDEN: "评论已被下架，不可互动",
        ResourceAuditStatusEnum.DELETED: "评论不存在或已删除",
    }

    def __init__(self, detail: str = "评论不存在或已删除") -> None:
        super().__init__(
            status_code=ResponseCode.NOT_FOUND,
            detail=detail,
            code=ResponseCode.NOT_FOUND,
        )

    @classmethod
    def for_state(cls, state: ResourceAuditStatusEnum) -> "CommentNotInteractiveException":
        """按评论生命周期状态构造异常，反馈对应状态的准确文案。"""
        return cls(cls._STATE_MSG.get(state, "评论不可互动"))


__all__ = ["CommentNotInteractiveException"]
