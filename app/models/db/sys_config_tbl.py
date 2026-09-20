"""运行时系统配置表（2.64.0）。

`msg_sys_config` 是「可热更新的运营参数」的落库位置：`key` 主键 + JSON `value`，
由 `app/services/common/runtime_config.py` 读取（进程内 TTL 缓存 + DB 权威）——
管理端改一次即全实例生效（写入实例立即、其余实例 ≤ TTL），**不引入 Redis**
（与「所有数据直接落 MySQL」的项目约定一致）。

`settings` 只表示「代码默认值 / 兜底」：表里没有该 key、查询失败或值非法时一律回落，
因此本表为空也不影响服务启动与业务可用性。

命名用 `msg_` 前缀（与 `msg_user_setting` / `msg_comment_*` 同族），而非互动域的 `T*`。
"""

from sqlalchemy import BIGINT, JSON
from sqlmodel import Column, Field

from app.models.db.base_tbl import TimestampMixin


class SysConfig(TimestampMixin, table=True):
    """运行时系统配置项（key → JSON value）。"""

    __tablename__ = "msg_sys_config"
    __table_args__ = ({"extend_existing": True},)

    key: str = Field(
        primary_key=True,
        max_length=64,
        description="配置键（如 comment_rate_limit）；同一 key 的值整体原子替换",
    )
    value: dict = Field(
        default_factory=dict,
        sa_column=Column(JSON, nullable=False),
        description="配置值（JSON）：结构由各配置项约定，写入前由读取器侧的模型校验",
    )
    remark: str | None = Field(
        default=None, max_length=255, description="备注（管理端展示，说明用途）"
    )
    updatedBy: int = Field(
        default=0, sa_type=BIGINT, description="最后修改该配置的管理员 mid（0=系统 / 未知）"
    )


__all__ = ["SysConfig"]
