"""内容发布「每日创建上限」的通用计数工具（2.58.0）。

设计说明：
- **计数口径**：统计「本自然日、指定作者 mid、满足可选过滤条件的创建行数」。
  不按软删 / 审核 / 驳回等状态过滤——因删除为软删（行保留、作者与创建时间不改写），
  故「创建后删除、再创建」也无法绕过当日上限（即「删除也算次数」）。
- **自治接入**：各 biz 自行 import 本工具，传入自己资源表的 `model`、作者列、
  创建时间列、可选附加过滤（如动态只计 WORD），配合自己的上限配置与业务异常使用；
  并非每个资源都必须接入本工具。
- 超限与否由调用方决定（如 `cnt >= limit`），本工具只负责「当天已创建多少」。
"""

from datetime import datetime, timedelta

from sqlmodel import col, func, select
from sqlmodel.ext.asyncio.session import AsyncSession


def _today_window() -> tuple[datetime, datetime]:
    """返回 [本自然日 0 点, 次日 0 点) 的 datetime 窗口。

    采用服务器本地自然日边界（与 dm 每日上限、seed 等一致），
    用 `datetime.combine` 构造 0 点整，避免时区 / 毫秒误差。
    """
    now = datetime.now()
    day_start = datetime.combine(now.date(), datetime.min.time())
    day_end = day_start + timedelta(days=1)
    return day_start, day_end


async def count_created_today(
    session: AsyncSession,
    model: type,
    *,
    author_column,
    author_mid: int,
    created_column=None,
    extra_conditions=(),
) -> int:
    """统计某资源表在**本自然日**由 ``author_mid`` 创建的记录数。

    Args:
        session: 会话。
        model: 资源表模型类（SQLModel `table=True`）。
        author_column: 作者 mid 对应列（如 `Model.mid` / `Model.creatorMid`）。
        author_mid: 当前作者 mid。
        created_column: 创建时间列（缺省回落到模型公共 ``created_at``）。
        extra_conditions: 额外过滤条件元组（如动态只计 WORD 用 `Model.dynType == ...`）。

    Returns:
        本自然日内该作者创建的记录条数（不含软删 / 审核等状态过滤）。
    """
    if created_column is None:
        created_column = getattr(model, "created_at")
    start, end = _today_window()
    stmt = select(func.count()).select_from(model).where(
        col(author_column) == author_mid,
        col(created_column) >= start,
        col(created_column) < end,
        *extra_conditions,
    )
    result = await session.exec(stmt)
    return int(result.one() or 0)


__all__ = ["count_created_today"]
