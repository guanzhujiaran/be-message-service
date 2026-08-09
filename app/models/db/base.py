"""数据库表模型公共基类与列类型工具。"""

from datetime import datetime
from enum import IntEnum, StrEnum

from sqlalchemy import Enum as SAEnum
from sqlalchemy import Integer
from sqlalchemy.types import TypeDecorator
from sqlmodel import Field, SQLModel


class TimestampMixin(SQLModel):
    """统一的创建 / 更新时间字段。"""

    created_at: datetime = Field(
        default_factory=datetime.now,
        nullable=False,
        index=True,
        description="创建时间",
    )
    updated_at: datetime = Field(
        default_factory=datetime.now,
        nullable=False,
        sa_column_kwargs={"onupdate": datetime.now},
        description="更新时间",
    )


def str_enum_type(enum_cls: type[StrEnum], length: int = 16) -> SAEnum:
    """把 StrEnum 映射成 VARCHAR 列。

    直接用 SQLModel 的默认行为会生成 MySQL 原生 ENUM 且**存成员名**（`LIKE`），
    带来两个麻烦：新增枚举值要改表结构；库里的字面量和接口 / 原生 SQL 里
    出现的 `like` 对不上。这里统一处理：

    - `native_enum=False`：落成 `VARCHAR(length)`，加枚举值不需要 DDL；
    - `values_callable`：存枚举的 **value**（`like`），与接口层保持一致；
    - 读取时 SQLAlchemy 仍会还原成枚举成员，业务代码可放心用 `is` 比较。
    """
    return SAEnum(
        enum_cls,
        native_enum=False,
        length=length,
        values_callable=lambda e: [m.value for m in e],
    )


class _IntEnumColumn(TypeDecorator):
    """把 IntEnum 映射成 INTEGER 列，存其 **value**（0/1/2…）。

    与 `str_enum_type` 同理，避免 MySQL 原生 ENUM：
    - `impl = Integer`：落库为整数，加枚举值无需 DDL；
    - 读写经 `value` 互转，业务侧仍拿到枚举成员，可用 `is` 比较。
    """

    impl = Integer

    # 类型状态固定、可安全进入 SQLAlchemy 语句缓存（消除 cache_ok 警告）
    cache_ok = True

    def __init__(self, enum_cls: type[IntEnum], **kw):
        self.enum_cls = enum_cls
        super().__init__(**kw)

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        return value.value if isinstance(value, IntEnum) else value

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        return self.enum_cls(value)


def int_enum_type(enum_cls: type[IntEnum]) -> _IntEnumColumn:
    """构造一个存 IntEnum value 的 INTEGER 列类型。"""
    return _IntEnumColumn(enum_cls)


__all__ = ["TimestampMixin", "str_enum_type", "int_enum_type"]
