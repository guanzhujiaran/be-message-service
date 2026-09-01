"""抽奖卡片（LOTTERY）资源类（2.48.0）。

行为与通用资源一致，唯一差异：2.40.0 起 **lottery（crawler 资源）不允许下架**——
仅记录举报 + 转审核，Feed 层 / RPC 处置均不执行，故覆盖 `hide()` 跳过。
"""

from loguru import logger

from app.models.enums import InteractionBizTypeEnum
from app.services.interaction_actions.common.generic_biz import GenericResourceBiz

__all__ = ["LotteryBiz"]


class LotteryBiz(GenericResourceBiz):
    """抽奖卡片资源。"""

    _biz_type = InteractionBizTypeEnum.LOTTERY

    async def hide(self, *, operator_mid: int = 0, **kwargs) -> None:
        """lottery 不允许下架：跳过全部处置（Feed 层 / RPC 均不执行）。"""
        logger.info(f"lottery 不允许下架，跳过处置: bizId={self.biz_id}")
