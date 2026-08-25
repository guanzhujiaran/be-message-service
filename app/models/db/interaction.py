"""通用交互计数表 ORM（2.17.0 新增，2.18.0 复用 bili-common 通用基类）。

`TInteractionStat` 承载**非动态资源**（lottery / rpa_*）的收藏 / 点赞 / 浏览计数；
动态资源的计数仍走 `TMomentStat`。字段与通用逻辑收口到 bili-common
（`InteractionStatBase` / `InteractionViewLogBase` + `InteractionStatService`），
本文件仅建立物理表。

统一「明细表幂等 + 计数原子 ±1」范式（计划书 §4.2 / §4.10）。
"""

from sqlalchemy import BIGINT, PrimaryKeyConstraint, UniqueConstraint
from sqlmodel import Field, SQLModel

from app.models.db.base import TimestampMixin
from bili_common.models.interaction_stat import InteractionStatBase, InteractionViewLogBase


class TInteractionStat(InteractionStatBase, TimestampMixin, table=True):
    """通用交互计数表：每种业务资源（bizType+bizId）一行计数。"""

    __tablename__ = "TInteractionStat"
    __table_args__ = (
        PrimaryKeyConstraint("bizType", "bizId", name="TInteractionStat_pkey"),
        {"extend_existing": True, "comment": "通用交互计数：非动态资源(bizType,bizId)的收藏/点赞/浏览计数"},
    )


class TInteractionViewLog(InteractionViewLogBase, TimestampMixin, table=True):
    """通用浏览去重表（2.23.0；2.42.0 每用户每资源一行）：非动态资源同用户同资源只一行，
    ``lastViewAt`` 记录最后访问时间，跨自然日再次访问才给 Stat.viewCount +1。"""

    __tablename__ = "TInteractionViewLog"
    __table_args__ = (
        PrimaryKeyConstraint("pk", name="TInteractionViewLog_pkey"),
        UniqueConstraint(
            "bizType", "bizId", "mid", name="TInteractionViewLog_bizType_bizId_mid_key"
        ),
        {"extend_existing": True, "comment": "通用浏览去重表：每用户每资源一行，lastViewAt 判自然日窗口"},
    )


__all__ = ["TInteractionStat", "TInteractionViewLog"]
