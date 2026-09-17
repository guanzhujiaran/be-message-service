"""消息管理端（评论 / 私信审核）的细粒度权限授权服务。

授权数据自包含在 be-message-service 的 `msg_admin` 表内，与 RPA 权限体系解耦：
- 仅 root 管理员可授权 / 撤销其他用户的消息管理端权限；
- 权限为 per-biz 位掩码权限字（`biz_perms`，值 0~7）；内容明文 root 专属，不占权限位。
"""

from bili_common.deps.permissions import normalize_biz_perms
from sqlmodel import func, select

from app.core.database import SessionDep
from app.models.db.admin_tbl import MessageAdmin


class MessageAdminService:
    @staticmethod
    async def grant(
        session: SessionDep,
        operator_mid: int,
        mid: int,
        biz_perms: dict[str, int] | None = None,
        note: str | None = None,
    ) -> MessageAdmin:
        """授予 / 更新某用户的消息管理端权限（仅 root 调用）。"""
        # 清洗权限：非法域丢弃、权限字截断到 0~7
        safe_perms = normalize_biz_perms(biz_perms)
        existing = (
            await session.exec(select(MessageAdmin).where(MessageAdmin.mid == mid))
        ).first()
        if existing is not None:
            existing.granted_by = operator_mid
            existing.biz_perms = safe_perms
            existing.note = note
            admin = existing
        else:
            admin = MessageAdmin(
                mid=mid,
                granted_by=operator_mid,
                biz_perms=safe_perms,
                note=note,
            )
            session.add(admin)
        await session.commit()
        await session.refresh(admin)
        return admin

    @staticmethod
    async def revoke(session: SessionDep, mid: int) -> bool:
        """撤销某用户的消息管理端权限，成功返回 True。"""
        existing = (
            await session.exec(select(MessageAdmin).where(MessageAdmin.mid == mid))
        ).first()
        if existing is None:
            return False
        await session.delete(existing)
        await session.commit()
        return True

    @staticmethod
    async def list_admins(
        session: SessionDep, page_num: int, page_size: int
    ) -> tuple[list[MessageAdmin], int]:
        """分页列出全部消息管理端管理员。"""
        total = (
            await session.exec(select(func.count()).select_from(MessageAdmin))
        ).one()
        items = (
            await session.exec(
                select(MessageAdmin)
                .order_by(MessageAdmin.id.desc())
                .offset((page_num - 1) * page_size)
                .limit(page_size)
            )
        ).all()
        return items, total

    @staticmethod
    async def get_status(
        session: SessionDep, mid: int
    ) -> tuple[bool, dict[str, int]]:
        """返回某用户是否为消息管理端管理员及其各域权限字。"""
        admin = (
            await session.exec(select(MessageAdmin).where(MessageAdmin.mid == mid))
        ).first()
        if admin is None:
            return False, {}
        return True, admin.biz_perms or {}


__all__ = ["MessageAdminService"]
