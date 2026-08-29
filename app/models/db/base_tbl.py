"""数据库表模型公共基类与列类型工具。"""

from datetime import datetime

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


__all__ = ["TimestampMixin"]
