"""RPA 资源标签（RPA_TAG）资源类（2.49.0）。"""

from bili_common.models import InteractionBizTypeEnum
from app.services.interaction_actions.common.generic_biz import GenericResourceBiz

__all__ = ["RpaTagBiz"]


class RpaTagBiz(GenericResourceBiz):
    """RPA 资源标签资源（审核同 RPA 其它内容，行为同通用资源）。

    标签本身无点赞/举报/收藏等互动语义，仅复用通用审核栈
    （/api/v1/audit/approve|reject → 经 RPA RPC review_resource 置正常/驳回）。
    """

    _biz_type = InteractionBizTypeEnum.RPA_TAG