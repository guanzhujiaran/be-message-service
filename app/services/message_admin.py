"""消息管理端（评论 / 私信审核）的细粒度权限授权服务。

授权数据自包含在 be-message-service 的 `msg_admin` 表内，与 RPA 权限体系解耦：
- 仅 root 管理员可授权 / 撤销其他用户的消息管理端权限；
- root 专属权限（查看内容明文 / 设置过审没过审）不可授予他人（落库前 sanitize）。
"""

from sqlmodel import func, select

from app.core.database import SessionDep
from app.models.db.admin import MessageAdmin
from bili_common.deps.permissions import sanitize_permissions


class MessageAdminService:
    @staticmethod
    async def grant(
        session: SessionDep,
        operator_mid: int,
        mid: int,
        permissions: list[str],
        note: str | None = None,
    ) -> MessageAdmin:
        """授予 / 更新某用户的消息管理端权限（仅 root 调用）。"""
        # 清洗权限：剔除 root 专属权限，确保非 root 管理员永远拿不到敏感权限
        safe_permissions = sanitize_permissions(permissions)
        existing = (
            await session.exec(select(MessageAdmin).where(MessageAdmin.mid == mid))
        ).first()
        if existing is not None:
            existing.granted_by = operator_mid
            existing.permissions = safe_permissions
            existing.note = note
            admin = existing
        else:
            admin = MessageAdmin(
                mid=mid,
                granted_by=operator_mid,
                permissions=safe_permissions,
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
    ) -> tuple[bool, list[str]]:
        """返回某用户是否为消息管理端管理员及其权限列表。"""
        admin = (
            await session.exec(select(MessageAdmin).where(MessageAdmin.mid == mid))
        ).first()
        if admin is None:
            return False, []
        return True, admin.permissions or []


__all__ = ["MessageAdminService"]
