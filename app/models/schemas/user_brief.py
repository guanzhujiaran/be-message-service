"""用户展示简档（对外输出模型，计划书 §5.12）。

用户主数据只有一份（pptr Postgres 的 TUserInfo / TUserDetail / TUserVip / TUserLevel），
但同一个用户在不同的可见范围内能展示的字段并不相同。

历史做法（**已废弃**）：拆成 ``UserBriefPublic`` 基类 + ``UserBriefPrivate`` 子类，
靠调用方在装配他人可见响应时记得调 ``to_public()`` 投影。实测证明这个约定不可靠——
Pydantic 对「已是模型实例的子对象」只做 isinstance 检查，private 实例赋给声明为
public 的字段**不会被裁剪**，私密字段仍会被序列化出去；一旦某个调用点漏调就漏数据。

现行做法：**单一输出模型 + 字段级标记 + 序列化期裁剪**。

- 公开字段与私域字段同处 :class:`UserBriefOut`，不再用继承表达可见性；
- 私域字段（``vip_due_date`` / ``exp`` / ``role`` / ``email``）以 ``Private()`` 标记；
- 序列化时由 :class:`~app.models.schemas.visibility.VisibilityMixin` 依据当前
  :class:`~app.core.viewer_context.ViewerContext` 决定是否输出：
  **本人**（``viewer_mid == mid``）或**管理员**可见，其余一律剥离；
- 嵌套在本模型内的位置（评论 member、私信对端、@ 用户项）**自动生效**，调用点无需投影。

数据仍直连 pptr 只读取得，本服务不冗余任何用户字段。
"""

from typing import Annotated

from sqlmodel import SQLModel

from app.models.schemas.base import auto_str
from app.models.schemas.visibility import Private, VisibilityMixin


@auto_str
class UserBriefOut(SQLModel, VisibilityMixin):
    """用户展示简档：他人可见字段 + 本人 / 管理员可见的私域字段。

    私域字段在他人视角下由序列化器自动剥离，装配层**不需要**（也不应该）
    再手动投影。
    """

    mid: int
    uname: str | None = None
    avatar: str | None = None
    level: int = 0
    vip_status: str | None = None
    vip_type: int = 0
    sex: str | None = None
    sign: str | None = None
    follower_count: int = 0
    following_count: int = 0
    like_count: int = 0
    nameplate_name: str | None = None
    nameplate_image: str | None = None
    nameplate_level: str | None = None
    official_title: str | None = None

    # ---- 私域：本人 / 管理员可见 ----
    # 大会员到期时间（毫秒时间戳），来自 TUserVip.vip_due_date
    vip_due_date: Annotated[int | None, Private()] = None
    # 当前累积经验，来自 TUserLevel.current_exp
    exp: Annotated[int | None, Private()] = None
    # 角色标识（level0..level6 / root），来自 TUserInfo.role
    role: Annotated[str | None, Private()] = None
    # 脱敏后的邮箱，来自 TUserDetail.email
    email: Annotated[str | None, Private()] = None


__all__ = ["UserBriefOut"]
