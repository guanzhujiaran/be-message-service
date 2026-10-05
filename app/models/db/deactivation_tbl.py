"""用户注销冷静期状态表（两阶段注销）。

为什么单独建 MySQL 表而不给 pptr Postgres 的 `TUserInfo` 加列：
- 用户主体（TUserInfo/TUserDetail/...）在 pptr Postgres，项目硬约束「只改 MySQL，
  pptr Postgres 不改结构」；
- 注销是 be-message 编排的业务流程，冷静期标记放本服务主库最内聚。

生命周期：
1. 提交注销 → upsert 一条 `status=cooling` 记录，`delete_after = deactivated_at + N 天`，
   本地数据全部保留、可恢复；冷静期内重新 Casdoor 登录即删除该记录（自动撤销）。
2. 到期任务物理删除本地数据后，把记录置为 `pending_casdoor`（Casdoor 删除的补偿凭证）。
3. Casdoor 删除成功后删除该记录；失败则累计 `retry_count` / 记录 `casdoor_delete_error`，
   由补偿任务重试。
"""

from datetime import datetime

from sqlalchemy import BIGINT
from sqlmodel import Field, SQLModel, UniqueConstraint

from app.models.db.base_tbl import TimestampMixin


class UserDeactivation(TimestampMixin, table=True):
    """用户注销状态（一个 mid 同一时刻只允许一条有效记录）。"""

    __tablename__ = "msg_user_deactivation"
    __table_args__ = (
        UniqueConstraint("mid", name="uq_user_deactivation_mid"),
        {"extend_existing": True},
    )

    id: int | None = Field(default=None, primary_key=True)
    mid: int = Field(sa_type=BIGINT, index=True, nullable=False, description="用户 mid")
    # Casdoor 用户名：到期物理删除本地主体后仍需据此构造 Casdoor User 调 delete-user
    user_name: str | None = Field(default=None, max_length=128, description="Casdoor name")
    # cooling=冷静期可撤销；pending_casdoor=本地已删、Casdoor 待删（补偿态）
    status: str = Field(
        default="cooling",
        max_length=16,
        index=True,
        nullable=False,
        description="cooling / pending_casdoor",
    )
    deactivated_at: datetime = Field(
        default_factory=datetime.now, nullable=False, description="提交注销时刻"
    )
    delete_after: datetime = Field(
        default_factory=datetime.now,
        index=True,
        nullable=False,
        description="到期时刻（=deactivated_at + 冷静期天数）",
    )
    casdoor_delete_error: str | None = Field(
        default=None, max_length=512, description="最近一次 Casdoor 删除失败原因"
    )
    retry_count: int = Field(default=0, nullable=False, description="Casdoor 删除重试次数")


__all__ = ["SQLModel", "UserDeactivation"]
