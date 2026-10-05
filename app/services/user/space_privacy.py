"""空间对外可见性服务（2.58.0）。

职责边界：
- **可见性开关的读写**：`TUserSpacePrivacy`（每用户一行，懒创建，默认全关）
  + 复用既有 `TUserFavoriteSetting.showFavorites`（收藏夹整体对外）；
- **按访问者裁剪空间资料**：`viewer == mid` 一律不过滤；否则被关闭的敏感字段
  **直接不下发**（而不是下发 null），让前端无需区分「无权限」与「无数据」；
- **上次登录信息**：读 pptr `TUserActInfoLog` 最近一条登录记录，IP 属地由 mmdb
  **读取时实时解析**（不入库，IP 变了属地自动跟着变）。

统计数字（关注数 / 粉丝数 / 获赞 / 动态 / 被访问次数）**固定展示、不设开关**，
因此本服务不参与它们的过滤。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col, select

from app.core.database import new_pptr_session
from app.models.db import TUserFavoriteSetting, TUserSpacePrivacy
from app.models.pptr_db import PptrUserActInfoLog
from app.models.schemas.space import (
    SpaceInfoResp,
    SpacePrivacyFlags,
    SpacePrivacyUpdateReq,
)
from app.services.infrastructure.geo_ip import lookup as geo_lookup
from app.services.moment.interaction import (
    BeMessageInteractionStatService as InteractionStatService,
)
from bili_common.models import InteractionBizTypeEnum

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
    """空间可见性开关 + 资料裁剪（静态方法集合）。"""

    # ==================== 开关读写 ====================

    @staticmethod
    async def get_flags(session: AsyncSession, target_mid: int) -> SpacePrivacyFlags:
        """读取目标用户的可见性开关（无记录 = 全关，收藏夹沿用默认展示）。"""
        privacy_row = (
            await session.exec(
                select(TUserSpacePrivacy).where(
                    col(TUserSpacePrivacy.mid) == int(target_mid)
                )
            )
        ).one_or_none()
        fav_row = (
            await session.exec(
                select(TUserFavoriteSetting).where(
                    col(TUserFavoriteSetting.mid) == int(target_mid)
                )
            )
        ).one_or_none()
        return SpacePrivacyFlags(
            show_personal_info=bool(privacy_row.show_personal_info)
            if privacy_row
            else False,
            show_login_info=bool(privacy_row.show_login_info) if privacy_row else False,
            show_follow_list=bool(privacy_row.show_follow_list)
            if privacy_row
            else False,
            show_like_list=bool(privacy_row.show_like_list) if privacy_row else False,
            show_fans_list=bool(privacy_row.show_fans_list) if privacy_row else False,
            # 收藏夹沿用既有设置：缺省默认展示（与隐私表默认全关相反，见 TUserSpacePrivacy 文档）
            show_favorites=bool(fav_row.showFavorites) if fav_row else True,
        )

    @staticmethod
    async def update_flags(
        session: AsyncSession, actor_mid: int, req: SpacePrivacyUpdateReq
    ) -> SpacePrivacyFlags:
        """整体覆盖当前用户的五个开关（get_or_create 懒创建），返回设置后的值。"""
        mid = int(actor_mid)
        row = (
            await session.exec(
                select(TUserSpacePrivacy).where(col(TUserSpacePrivacy.mid) == mid)
            )
        ).one_or_none()
        if row is None:
            row = TUserSpacePrivacy(mid=mid)
        row.show_personal_info = bool(req.show_personal_info)
        row.show_login_info = bool(req.show_login_info)
        row.show_follow_list = bool(req.show_follow_list)
        row.show_like_list = bool(req.show_like_list)
        row.show_fans_list = bool(req.show_fans_list)
        session.add(row)
        await session.commit()
        return await SpacePrivacyService.get_flags(session, mid)

    # ==================== 资料裁剪 ====================

    @staticmethod
    def can_see_private(target_mid: int, viewer_mid: int | None) -> bool:
        """访问者是否「本人」（本人恒可见敏感字段；他人按开关，见 apply_flags_to_info）。"""
        return viewer_mid is not None and int(viewer_mid) == int(target_mid)

    @classmethod
    def apply_flags_to_info(
        cls,
        info: SpaceInfoResp,
        flags: SpacePrivacyFlags,
        target_mid: int,
        viewer_mid: int | None,
    ) -> None:
        """按开关裁剪 `SpaceInfoResp` 的敏感字段（原地修改）。

        - 本人：全部保留；
        - 他人且开关关闭：置 None，配合路由的 ``response_model_exclude_none=True``
          让字段**不出现在响应里**（前端不需要猜null 是没权限还是没数据）。
        """
        if cls.can_see_private(target_mid, viewer_mid):
            return
        if not flags.show_personal_info:
            info.email = None
        if not flags.show_login_info:
            info.last_login_at = None
            info.ip_location = None

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
        except Exception as exc:  # noqa: BLE001 —— pptr 不可用时空间页仍要能打开
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
        except Exception as exc:  # noqa: BLE001 —— 解析失败不应让空间页报错
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
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[space_privacy] 读取空间访问次数失败（降级为 0）: {exc}")
            return 0
        return int(counts.get(int(target_mid), {}).get("viewCount", 0))


__all__ = ["SpaceLoginInfo", "SpacePrivacyService"]
