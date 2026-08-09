"""pptr（be-gateway）Postgres 库 `PTR_Bili_Lot` 的模型兼容层。

**历史**：早期本文件直接定义 `TUserInfo` 等 4 张表的裁剪视图（并把 `createdAt` 重命名为
`created_at`），挂隔离的 `pptr_metadata` 只读映射。

**现状**：be-message 已**彻底接管** `PTR_Bili_Lot`（可读可写），完整模型统一放在
`app/models/pptr_db.py`（挂载 `SQLModel.metadata`，由 Alembic 管理版本演进）。本文件仅做
**兼容 re-export**，让既有 `from app.models.pptr_user import PptrUserInfo` 等 import 不破。

如需新增/调整 pptr 库表结构，请直接编辑 `app/models/pptr_db.py`。
"""

from app.models.pptr_db import (
    PptrUserInfo,
    PptrUserDetail,
    PptrUserLevel,
    PptrUserVip,
)

__all__ = [
    "PptrUserInfo",
    "PptrUserDetail",
    "PptrUserLevel",
    "PptrUserVip",
]
