"""pptr 用户领域删除（`cleanup_pptr`）。

物理删除 pptr Postgres 中指定 uid 用户的账号数据（不可恢复）：

- 日志表：`TUserActInfoLog`/`TUserExpRecord`/`TUserNameRecord`/`TUserPwdRecord`(mid)；
- 用户四表：`TUserLevel`/`TUserVip`/`TUserDetail`(mid) → `TUserInfo`(uid)。

**顺序关键**：所有 FK 无 `ondelete` 级联，必须按依赖逆序手动删——
先子表（Level/Vip/Detail）后主表（Info），日志表单独删。

**为何保留裸 SQL**：pptr 表（`TUserInfo` 等）由 Node（RPA-Browser）维护，
Python 侧 **没有对应的 SQLModel 模型**（bili-common 无定义），无法用
`delete(Model).where(...)`，只能原生 SQL。且 Postgres 表名是混合大小写，
必须用双引号 `"TUserInfo"` 精确匹配（未加引号会被折叠为小写报
`relation does not exist`）。

注意：本模块操作的是 **pptr Postgres engine**，调用方传入 `new_pptr_session()`
得到的 `AsyncSession`（与 be-message MySQL 的 session 不同库）。
"""

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

# 注意：pptr 表名为混合大小写（TUserInfo 等），Postgres 原生 SQL 中未加引号
# 的表名会被折叠为小写（tuserinfo），导致 `relation does not exist`。
# 因此所有表名必须用双引号 `"TUserInfo"` 精确匹配。
_SQLS = [
    # 日志表（mid 关联，FK 无 CASCADE/SET NULL 或需显式删）
    'DELETE FROM "TUserActInfoLog" WHERE mid = :uid',
    'DELETE FROM "TUserExpRecord" WHERE mid = :uid',
    'DELETE FROM "TUserNameRecord" WHERE mid = :uid',
    'DELETE FROM "TUserPwdRecord" WHERE mid = :uid',
    # 用户四表（逆依赖：Level/Vip -> Detail -> Info）
    'DELETE FROM "TUserLevel" WHERE mid = :uid',
    'DELETE FROM "TUserVip" WHERE mid = :uid',
    'DELETE FROM "TUserDetail" WHERE mid = :uid',
    'DELETE FROM "TUserInfo" WHERE uid = :uid',
]


class CleanupPptrService:
    """pptr 用户领域删除（静态方法集合）。"""

    @staticmethod
    async def delete_all_by_uid(session: AsyncSession, uid: int) -> None:
        """物理删除 pptr Postgres 中指定 uid 的账号数据（调用方负责 commit）。"""
        for sql in _SQLS:
            await session.exec(text(sql), params={"uid": uid})


__all__ = ["CleanupPptrService"]
