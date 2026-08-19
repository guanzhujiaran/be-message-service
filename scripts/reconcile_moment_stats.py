"""动态统计夜间对账脚本（Phase 4 P4-T6，非热路径）。

用明细表 COUNT 校正 TMomentStat 计数，修复并发/异常导致的计数漂移：
- likeCount  ← TMomentLike 行数
- viewCount  ← TMomentViewLog 各行 viewCount 之和
- repostCount 由状态机维护，此处不主动改（避免与审核流冲突）

**仅用于夜间定时 / 运维手动执行**，严禁在请求链路调用。

用法（项目根目录下）：
    uv run python scripts/reconcile_moment_stats.py
"""

import asyncio
import sys
from pathlib import Path

# 允许直接 `uv run python scripts/reconcile_moment_stats.py` 运行（不依赖手动 PYTHONPATH）
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from loguru import logger

from app.core.database import new_session
from app.services.moment_stat import MomentStatService


async def main() -> None:
    logger.info("开始动态统计对账…")
    async with new_session() as session:
        fixed = await MomentStatService.reconcile(session)
    logger.info(f"对账完成，修正统计: {fixed}")


if __name__ == "__main__":
    asyncio.run(main())
