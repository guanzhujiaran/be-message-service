"""空间对外可见性服务（2.58.0）。

职责边界：
- **可见性开关的读写**：`TUserSpacePrivacy`（每用户一行，懒创建，默认全关）；
  （收藏不在本服务内 —— 2026-10-08 起改为逐夹 `TFavoriteFolder.is_public`）
- **构造可见性上下文**：`visibility_for()` 返回 `SpaceVisibility`，裁剪与门控都由它统一判定
  （规则见 `space/attributes.py` 的属性描述符）；本人一律不过滤；
- **上次登录信息**：读 pptr `TUserActInfoLog` 最近一条登录记录，IP 属地由 mmdb
  **读取时实时解析**（不入库，IP 变了属地自动跟着变）。

统计数字（关注数 / 粉丝数 / 获赞 / 动态 / 被访问次数）**固定展示、不设开关**，
因此本服务不参与它们的过滤。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from bili_common.models import InteractionBizTypeEnum
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col, select

from app.core.database import new_pptr_session
from app.models.db import TUserSpacePrivacy
from app.models.pptr_db import PptrUserActInfoLog
from app.models.schemas.space import (
    SpacePrivacyFlags,
    SpacePrivacyUpdateReq,
)
from app.services.infrastructure.geo_ip import lookup as geo_lookup
from app.services.moment.interaction import (
    BeMessageInteractionStatService as InteractionStatService,
)
from app.services.user.space.attributes import SPACE_ATTRIBUTES
from app.services.user.space.visibility import SpaceVisibility

# 登录记录的 act_info 取值（写入点见 PptrUser.get_user_nav_data / casdoor_service）
_ACT_INFO_LOGIN = ("login_succ", "daily_login")

#: `geo_ip.lookup` 对无法解析的 IP（内网 / 畸形 / 保留段）返回的占位文案
_UNKNOWN_LOCATION = "未知"


@dataclass(slots=True)
class SpaceLoginInfo:
    """空间主的最近一次登录信息（读取时解析，不入库）。"""

    last_login_at: datetime | None = None
    ip_location: str | None = None


class SpacePrivacyService:
    """空间可见性开关读写 + 可见性上下文构造（静态方法集合）。

    「有哪些维度、默认值是什么、关闭后怎么裁剪」全部由 `space/attributes.py` 的
    属性描述符声明，本服务只负责**存取**与**装配上下文**：

    - `get_flags`：把 `TUserSpacePrivacy` 的值按注册表读成
      `SpacePrivacyFlags`（无记录时用属性自带的缺省值，不再硬编码 False/True）；
    - `update_flags`：按注册表逐项写回隐私表（加维度不用改这里）；
    - `visibility_for`：一次请求构造一次 `SpaceVisibility`，之后所有可见性判断都问它。
    """

    # ==================== 开关读写 ====================

    @staticmethod
    async def get_flags(session: AsyncSession, target_mid: int) -> SpacePrivacyFlags:
        """读取目标用户的可见性开关（无记录 = 用各属性自带的缺省值）。"""
        mid = int(target_mid)
        privacy_row = (
            await session.exec(
                select(TUserSpacePrivacy).where(col(TUserSpacePrivacy.mid) == mid)
            )
        ).one_or_none()
        flags = SpacePrivacyFlags()
        for attr in SPACE_ATTRIBUTES:
            value = (
                attr.default_value()
                if privacy_row is None
                else bool(
                    getattr(privacy_row, attr.spec.db_column, attr.default_value())
                )
            )
            setattr(flags, str(attr.key.value), value)
        return flags

    @staticmethod
    async def update_flags(
        session: AsyncSession, actor_mid: int, req: SpacePrivacyUpdateReq
    ) -> SpacePrivacyFlags:
        """整体覆盖当前用户的隐私开关（get_or_create 懒创建），返回设置后的值。

        只写 `TUserSpacePrivacy` 承载的维度（收藏的开关有独立接口），
        逐项由注册表驱动 —— 新增维度 **无需**改本方法。
        """
        mid = int(actor_mid)
        row = (
            await session.exec(
                select(TUserSpacePrivacy).where(col(TUserSpacePrivacy.mid) == mid)
            )
        ).one_or_none()
        if row is None:
            row = TUserSpacePrivacy(mid=mid)
        for attr in SPACE_ATTRIBUTES:
            column = attr.spec.db_column
            setattr(row, column, bool(getattr(req, column, False)))
        session.add(row)
        await session.commit()
        return await SpacePrivacyService.get_flags(session, mid)

    # ==================== 可见性上下文 ====================

    @staticmethod
    def can_see_private(target_mid: int, viewer_mid: int | None) -> bool:
        """访问者是否「本人」（本人恒可见敏感字段）。"""
        return viewer_mid is not None and int(viewer_mid) == int(target_mid)

    @staticmethod
    async def visibility_for(
        session: AsyncSession, owner_mid: int, viewer_mid: int | None
    ) -> SpaceVisibility:
        """构造一次请求的可见性上下文（读一次开关表，后续判定不再打库）。"""
        flags = await SpacePrivacyService.get_flags(session, int(owner_mid))
        return SpaceVisibility(
            owner_mid=int(owner_mid), viewer_mid=viewer_mid, flags=flags
        )

    # ==================== 上次登录信息（pptr + mmdb） ====================

    @staticmethod
    async def load_login_info(target_mid: int) -> SpaceLoginInfo:
        """取空间主最近一次登录记录，并实时解析 IP 属地（失败降级为空，不阻塞空间页）。

        `TUserActInfoLog` 目前只有主键与外键、没有 `(mid, createdAt)` 索引，
        按 mid 取最新一条会全表扫（见需求文档 §3.1 的索引待办）；这里用
        `LIMIT 1` 先把单次代价压到最小。
        """
        try:
            async with new_pptr_session() as s:
                row = (
                    await s.exec(
                        select(PptrUserActInfoLog)
                        .where(
                            col(PptrUserActInfoLog.mid) == int(target_mid),
                            col(PptrUserActInfoLog.deletedAt).is_(None),
                            col(PptrUserActInfoLog.act_info).in_(_ACT_INFO_LOGIN),
                        )
                        .order_by(col(PptrUserActInfoLog.createdAt).desc())
                        .limit(1)
                    )
                ).first()
        except Exception as exc:
            logger.warning(f"[space_privacy] 读取最近登录记录失败（降级为空）: {exc}")
            return SpaceLoginInfo()
        if row is None:
            return SpaceLoginInfo()
        return SpaceLoginInfo(
            last_login_at=row.createdAt,
            ip_location=SpacePrivacyService._resolve_location(row.ip),
        )

    @staticmethod
    def _resolve_location(raw_ip: str | None) -> str | None:
        """IP → 属地文案（「省 市」+ 运营商，失败返回 None）。

        与评论 / 动态的 `ip_location` 同一口径（`geo_ip.lookup` 是懒加载单例，开销可忽略）；
        这里**不返回 IP 本身**，只给属地。

        注意：`geo_ip` 对无法解析的 IP（内网 / 保留段 / 畸形）返回「未知」而非抛异常，
        直接透传会让空间页显示「未知」这种负信息，因此这里把它视为**无数据**（返回 None，
        前端不渲染该行）。
        """
        if not raw_ip:
            return None
        try:
            geo = geo_lookup(raw_ip)
        except Exception as exc:
            logger.warning(f"[space_privacy] IP 属地解析失败: {exc}")
            return None
        parts = [p for p in (geo.poi, geo.isp) if p and p != _UNKNOWN_LOCATION]
        return " ".join(parts) if parts else None

    # ==================== 空间被访问次数 ====================

    @staticmethod
    async def get_view_count(session: AsyncSession, target_mid: int) -> int:
        """空间被访问次数（`TInteractionStat(bizType=USER)` 的 viewCount）。

        统计数字固定展示、不设开关；读取失败降级为 0，不阻塞空间资料。
        """
        try:
            counts = await InteractionStatService.batch_get_counts(
                session, InteractionBizTypeEnum.USER, [int(target_mid)]
            )
        except Exception as exc:
            logger.warning(f"[space_privacy] 读取空间访问次数失败（降级为 0）: {exc}")
            return 0
        return int(counts.get(int(target_mid), {}).get("viewCount", 0))


__all__ = ["SpaceLoginInfo", "SpacePrivacyService"]
