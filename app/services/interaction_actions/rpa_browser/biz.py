"""RPA 浏览器实例（RPA_BROWSER）资源类（2.48.0）。

浏览器指纹 / 实例是**用户私有资源**（不对外公开、无社区广场），故**不支持点赞 / 收藏**：
两者在本类覆盖为业务错误（400），其余举报 / 审核等沿用通用资源行为。
"""

from bili_common.models import InteractionBizTypeEnum
from app.services.interaction_actions.base import InteractionActionError
from app.services.interaction_actions.common.generic_biz import GenericResourceBiz

__all__ = ["RpaBrowserBiz"]


class RpaBrowserBiz(GenericResourceBiz):
    """RPA 浏览器实例资源（私有资源：不支持点赞 / 收藏）。"""

    _biz_type = InteractionBizTypeEnum.RPA_BROWSER

    async def like(self, **kwargs):
        """点赞：浏览器指纹为用户私有资源，不支持。"""
        raise InteractionActionError("浏览器指纹资源不支持点赞")

    async def favorite(self, **kwargs):
        """收藏：浏览器指纹为用户私有资源，不支持。"""
        raise InteractionActionError("浏览器指纹资源不支持收藏")
