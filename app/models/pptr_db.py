"""pptr（be-gateway）Postgres 库 `PTR_Bili_Lot` 的**完整接管模型**。

本文件由 `sqlacodegen` 反向 `PTR_Bili_Lot` 后，手工改写为项目统一的 SQLModel 风格，
并挂载到 `SQLModel.metadata`（与 be-message 的 MySQL 元数据合并，由 `alembic_pptr/`
分支统一纳管 public schema 下所有 pptr 表，做版本演进）。

约束：
- 列名（`name`）严格对齐数据库真实结构（camelCase 软删/时间戳列原样保留）；
- Python 属性名与数据库列名完全一致；
- 原库 `TUserDetail/TUserLevel/TUserVip` 是 `TUserInfo` 的 joined/单表继承子类，
  此处**拍平为独立表**（各自完整列 + `mid` 关联），与 `PptrUserService` 的
  `mid == uid` 关联查询方式一致，规避 SQLModel 对 SQLAlchemy 继承映射的支持缺失。
"""

from datetime import datetime

from sqlalchemy import (
    BIGINT,
    DateTime,
    ForeignKeyConstraint,
    Index,
    JSON,
    PrimaryKeyConstraint,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlmodel import Field, SQLModel

# ============================ 用户主数据（继承拍平）============================


class PptrUserInfo(SQLModel, table=True):
    __tablename__ = "TUserInfo"
    __table_args__ = (
        PrimaryKeyConstraint("uid", name="TUserInfo_pkey"),
        UniqueConstraint("user_name", name="TUserInfo_user_name_key"),
    )

    uid: int = Field(default=None, primary_key=True, sa_type=BIGINT)
    createdAt: datetime = Field(
        default=None,
        nullable=False,
        sa_type=DateTime(timezone=True),
        sa_column_kwargs={"server_default": text("CURRENT_TIMESTAMP")},
    )
    updatedAt: datetime = Field(
        default=None,
        nullable=False,
        sa_type=DateTime(timezone=True),
        sa_column_kwargs={"server_default": text("CURRENT_TIMESTAMP"), "onupdate": func.now()},
    )
    user_name: str | None = Field(default=None, sa_type=Text)
    pwd: str | None = Field(default=None, sa_type=Text)
    role: str | None = Field(
        default=None,
        max_length=255,
        sa_column_kwargs={"server_default": text("'level0'"), "comment": "level0\nlevel1\n...\nroot"},
    )
    deletedAt: datetime | None = Field(default=None, sa_type=DateTime(timezone=True))
    reg_ip_info_id: int | None = Field(default=None, sa_type=BIGINT)


class PptrUserDetail(SQLModel, table=True):
    __tablename__ = "TUserDetail"
    __table_args__ = (
        ForeignKeyConstraint(["mid"], ["TUserInfo.uid"], onupdate="CASCADE", name="TUserDetail_mid_fkey"),
        PrimaryKeyConstraint("mid", name="TUserDetail_pkey"),
        UniqueConstraint("uname", name="TUserDetail_uname_key"),
    )

    mid: int = Field(default=None, primary_key=True, sa_type=BIGINT, sa_column_kwargs={"autoincrement": False})
    createdAt: datetime = Field(
        default=None,
        nullable=False,
        sa_type=DateTime(timezone=True),
        sa_column_kwargs={"server_default": text("CURRENT_TIMESTAMP")},
    )
    updatedAt: datetime = Field(
        default=None,
        nullable=False,
        sa_type=DateTime(timezone=True),
        sa_column_kwargs={"server_default": text("CURRENT_TIMESTAMP"), "onupdate": func.now()},
    )
    avatar: str | None = Field(default=None, max_length=1024)
    uname: str | None = Field(default=None, max_length=50)
    sign: str | None = Field(default=None, max_length=1024)
    sex: str | None = Field(default=None, max_length=50)
    deletedAt: datetime | None = Field(default=None, sa_type=DateTime(timezone=True))
    birthday: datetime | None = Field(
        default=None,
        sa_type=DateTime(timezone=True),
        sa_column_kwargs={"server_default": text("'1970-01-01 00:00:00+08'")},
    )
    email: str | None = Field(default=None, max_length=255)


class PptrUserLevel(SQLModel, table=True):
    __tablename__ = "TUserLevel"
    __table_args__ = (
        ForeignKeyConstraint(["mid"], ["TUserDetail.mid"], onupdate="CASCADE", name="TUserLevel_mid_fkey"),
        PrimaryKeyConstraint("mid", name="TUserLevel_pkey"),
    )

    mid: int = Field(default=None, primary_key=True, sa_type=BIGINT, sa_column_kwargs={"autoincrement": False})
    createdAt: datetime = Field(
        default=None,
        nullable=False,
        sa_type=DateTime(timezone=True),
        sa_column_kwargs={"server_default": text("CURRENT_TIMESTAMP")},
    )
    updatedAt: datetime = Field(
        default=None,
        nullable=False,
        sa_type=DateTime(timezone=True),
        sa_column_kwargs={"server_default": text("CURRENT_TIMESTAMP"), "onupdate": func.now()},
    )
    current_exp: int | None = Field(default=None, sa_type=BIGINT, sa_column_kwargs={"server_default": text("0")})
    current_level: int | None = Field(default=None, sa_type=BIGINT, sa_column_kwargs={"server_default": text("0")})
    current_min: int | None = Field(default=None, sa_type=BIGINT, sa_column_kwargs={"server_default": text("0")})
    deletedAt: datetime | None = Field(default=None, sa_type=DateTime(timezone=True))


class PptrUserVip(SQLModel, table=True):
    __tablename__ = "TUserVip"
    __table_args__ = (
        ForeignKeyConstraint(["mid"], ["TUserDetail.mid"], onupdate="CASCADE", name="TUserVip_mid_fkey"),
        PrimaryKeyConstraint("mid", name="TUserVip_pkey"),
    )

    mid: int = Field(default=None, primary_key=True, sa_type=BIGINT, sa_column_kwargs={"autoincrement": False})
    createdAt: datetime = Field(
        default=None,
        nullable=False,
        sa_type=DateTime(timezone=True),
        sa_column_kwargs={"server_default": text("CURRENT_TIMESTAMP")},
    )
    updatedAt: datetime = Field(
        default=None,
        nullable=False,
        sa_type=DateTime(timezone=True),
        sa_column_kwargs={"server_default": text("CURRENT_TIMESTAMP"), "onupdate": func.now()},
    )
    vip_due_date: int | None = Field(
        default=None,
        sa_column_kwargs={"server_default": text("0"), "comment": "vip到期时间戳（ms"},
    )
    vip_pay_type: int | None = Field(
        default=None,
        sa_column_kwargs={"server_default": text("0"), "comment": "大致分成不同充值渠道？"},
    )
    vip_status: int | None = Field(
        default=None,
        sa_column_kwargs={"server_default": text("0"), "comment": "0：非vip\n1：目前就是vip\n2：非VIP（充值过，过期了）"},
    )
    vip_type: int | None = Field(
        default=None,
        sa_column_kwargs={"server_default": text("0"), "comment": "0：非vip\n1：月度\n2：年度\n3：十年\n4：百年"},
    )
    deletedAt: datetime | None = Field(default=None, sa_type=DateTime(timezone=True))


class PptrUserActInfoLog(SQLModel, table=True):
    __tablename__ = "TUserActInfoLog"
    __table_args__ = (
        ForeignKeyConstraint(["mid"], ["TUserInfo.uid"], ondelete="SET NULL", name="TUserActInfoLog_mid_fkey"),
        PrimaryKeyConstraint("pk", name="TUserActInfoLog_pkey"),
        {"comment": "用户行为日志，记录ip，ua，headers等信息？"},
    )

    pk: int = Field(default=None, primary_key=True, sa_type=BIGINT, sa_column_kwargs={"autoincrement": True})
    createdAt: datetime = Field(
        default=None,
        nullable=False,
        sa_type=DateTime(timezone=True),
        sa_column_kwargs={"server_default": text("CURRENT_TIMESTAMP")},
    )
    updatedAt: datetime = Field(
        default=None,
        nullable=False,
        sa_type=DateTime(timezone=True),
        sa_column_kwargs={"server_default": text("CURRENT_TIMESTAMP"), "onupdate": func.now()},
    )
    mid: int | None = Field(default=None, sa_type=BIGINT)
    ip: str | None = Field(default=None, max_length=50)
    ua: str | None = Field(default=None, sa_type=Text)
    headers: dict | None = Field(default=None, sa_type=JSON)
    act_info: str | None = Field(default=None, sa_type=Text)
    deletedAt: datetime | None = Field(default=None, sa_type=DateTime(timezone=True))


# ============================ 用户经验/密码记录 ============================


class PptrUserExpRecord(SQLModel, table=True):
    __tablename__ = "TUserExpRecord"
    __table_args__ = (
        ForeignKeyConstraint(["mid"], ["TUserInfo.uid"], name="TUserExpRecord_mid_fkey"),
        PrimaryKeyConstraint("pk", name="TUserExpRecord_pkey"),
        Index("idx_exp_record_mid_action_ref_date", "mid", "action_type", "ref_date"),
        {"comment": "用户经验增加记录表，记录所有行为（每日登录、发评论等）增加的经验值"},
    )

    pk: int = Field(default=None, primary_key=True, sa_type=BIGINT, sa_column_kwargs={"autoincrement": True})
    mid: int = Field(default=None, nullable=False, sa_type=BIGINT)
    createdAt: datetime = Field(
        default=None,
        nullable=False,
        sa_type=DateTime(timezone=True),
        sa_column_kwargs={"server_default": text("CURRENT_TIMESTAMP")},
    )
    action_type: int = Field(
        default=None,
        nullable=False,
        sa_column_kwargs={"comment": "行为类型 int：1=daily_login（每日登录），与 ExpActionType 枚举对应"},
    )
    exp: int = Field(
        default=None,
        nullable=False,
        sa_column_kwargs={"server_default": text("0"), "comment": "本次增加的经验值"},
    )
    ref_date: str = Field(
        default=None,
        max_length=10,
        nullable=False,
        sa_column_kwargs={"comment": "行为引用日期，格式 YYYY-MM-DD，用于每日/每周行为幂等检查"},
    )


class PptrUserNameRecord(SQLModel, table=True):
    __tablename__ = "TUserNameRecord"
    __table_args__ = (
        ForeignKeyConstraint(["mid"], ["TUserInfo.uid"], name="TUserNameRecord_mid_fkey"),
        PrimaryKeyConstraint("pk", name="TUserNameRecord_pkey"),
    )

    pk: int = Field(default=None, primary_key=True, sa_type=BIGINT, sa_column_kwargs={"autoincrement": True})
    createdAt: datetime = Field(
        default=None,
        nullable=False,
        sa_type=DateTime(timezone=True),
        sa_column_kwargs={"server_default": text("CURRENT_TIMESTAMP")},
    )
    updatedAt: datetime = Field(
        default=None,
        nullable=False,
        sa_type=DateTime(timezone=True),
        sa_column_kwargs={"server_default": text("CURRENT_TIMESTAMP"), "onupdate": func.now()},
    )
    mid: int = Field(default=None, nullable=False, sa_type=BIGINT)
    prev_uname: str | None = Field(default=None, max_length=50)
    deletedAt: datetime | None = Field(default=None, sa_type=DateTime(timezone=True))


class PptrUserPwdRecord(SQLModel, table=True):
    __tablename__ = "TUserPwdRecord"
    __table_args__ = (
        ForeignKeyConstraint(["mid"], ["TUserInfo.uid"], name="TUserPwdRecord_mid_fkey"),
        PrimaryKeyConstraint("pk", name="TUserPwdRecord_pkey"),
    )

    pk: int = Field(default=None, primary_key=True, sa_type=BIGINT, sa_column_kwargs={"autoincrement": True})
    createdAt: datetime = Field(
        default=None,
        nullable=False,
        sa_type=DateTime(timezone=True),
        sa_column_kwargs={"server_default": text("CURRENT_TIMESTAMP")},
    )
    updatedAt: datetime = Field(
        default=None,
        nullable=False,
        sa_type=DateTime(timezone=True),
        sa_column_kwargs={"server_default": text("CURRENT_TIMESTAMP"), "onupdate": func.now()},
    )
    mid: int | None = Field(default=None, sa_type=BIGINT)
    prev_pwd: str | None = Field(default=None, max_length=255)
    deletedAt: datetime | None = Field(default=None, sa_type=DateTime(timezone=True))


__all__ = [
    "PptrUserInfo",
    "PptrUserDetail",
    "PptrUserLevel",
    "PptrUserVip",
    "PptrUserActInfoLog",
    "PptrUserExpRecord",
    "PptrUserNameRecord",
    "PptrUserPwdRecord",
]