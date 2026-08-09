"""Phase E1 — 枚举值存取往返测试。

验证「枚举列落库存 value、读回还原成枚举成员」这条约定在真实 MySQL 上成立：

- `StrEnum` 列（`str_enum_type`）→ `VARCHAR`，库里存 **值**（`like` / `published` / `stranger`），
  不是成员名（`LIKE` / `PUBLISHED` / `STRANGER`），也不是 MySQL 原生 ENUM。
- `IntEnum` 列（`int_enum_type`）→ `INTEGER`，库里存 **整数值**（0 / 1 / 2）。
- ORM 读回后必须是枚举成员本身，业务代码里的 `is` 比较才成立。

之所以要专门测：SQLModel 默认会把 `StrEnum` 落成原生 ENUM 且存成员名，
一旦回退，库里字面量会与接口层 / 原生 SQL 里的小写值对不上，查询会静默全错。
"""

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlmodel import select, text
from sqlmodel.ext.asyncio.session import AsyncSession as SQLModelAsyncSession

from app.core import database as db_mod
from app.core.config import settings
from app.core.database import new_session
from app.models.db import (
    DmMessageIndex,
    DmSession,
    EventMessage,
    NotifyMessage,
)
from app.models.enums import (
    DmMsgStatusEnum,
    DmMsgTypeEnum,
    DmRelationEnum,
    DmSessionTypeEnum,
    EventTypeEnum,
    NotifyLevelEnum,
    NotifyStatusEnum,
    NotifyTargetTypeEnum,
    SourceTypeEnum,
)


@pytest.fixture(autouse=True)
async def _bind_engine_per_test():
    """每个测试在自己的事件循环里重建 engine，避免跨循环复用报错。"""
    engine = create_async_engine(
        settings.mysql_message_url,
        pool_pre_ping=True,
        future=True,
        connect_args={"charset": "utf8mb4", "autocommit": False},
    )
    db_mod.engine = engine
    db_mod.async_session_maker = async_sessionmaker(
        bind=engine,
        class_=SQLModelAsyncSession,
        expire_on_commit=False,
        autoflush=False,
    )
    yield
    await engine.dispose()


# 独立 mid 区间，避免与 Phase B / C 用例互相干扰
E_MID = 900070
E_TALKER = 900071
E_MSGKEY = 999_000_000_070


async def _cleanup() -> None:
    async with new_session() as s:
        await s.exec(text(f"DELETE FROM msg_notify WHERE creator_mid = {E_MID}"))
        await s.exec(text(f"DELETE FROM msg_event WHERE mid = {E_MID}"))
        await s.exec(text(f"DELETE FROM msg_dm_session WHERE owner_mid = {E_MID}"))
        await s.exec(text(f"DELETE FROM msg_dm_index WHERE owner_mid = {E_MID}"))
        await s.commit()


async def test_str_enum_round_trip() -> None:
    """StrEnum 列：库里是小写 value，ORM 读回是枚举成员。"""
    await _cleanup()
    async with new_session() as s:
        row = NotifyMessage(
            title="enum-rt",
            content="c",
            target_type=NotifyTargetTypeEnum.LEVEL,
            target_value="5",
            level=NotifyLevelEnum.URGENT,
            status=NotifyStatusEnum.PUBLISHED,
            creator_mid=E_MID,
        )
        s.add(row)
        await s.commit()
        await s.refresh(row)
        nid = row.id

        # 1) 原生 SQL 看库里的字面量：必须是 value（小写），不是成员名
        raw = (
            await s.exec(
                text(
                    "SELECT target_type, level, status FROM msg_notify "
                    f"WHERE id = {nid}"
                )
            )
        ).one()
        assert raw[0] == "level", f"target_type 应存 value，实际 {raw[0]!r}"
        assert raw[1] == "urgent", f"level 应存 value，实际 {raw[1]!r}"
        assert raw[2] == "published", f"status 应存 value，实际 {raw[2]!r}"

        # 2) ORM 读回：还原成枚举成员，可用 is 比较
        s.expunge_all()
        got = (
            await s.exec(select(NotifyMessage).where(NotifyMessage.id == nid))
        ).one()
        assert got.target_type is NotifyTargetTypeEnum.LEVEL
        assert got.level is NotifyLevelEnum.URGENT
        assert got.status is NotifyStatusEnum.PUBLISHED

        # 3) 用枚举成员做 WHERE 过滤能命中（绑定参数同样按 value 下发）
        hit = (
            await s.exec(
                select(NotifyMessage).where(
                    NotifyMessage.creator_mid == E_MID,
                    NotifyMessage.status == NotifyStatusEnum.PUBLISHED,
                )
            )
        ).all()
        assert [h.id for h in hit] == [nid], "按枚举成员过滤应命中该行"
    await _cleanup()


async def test_str_enum_column_is_varchar_not_native_enum() -> None:
    """枚举列的物理类型必须是 VARCHAR，不能回退成 MySQL 原生 ENUM。"""
    async with new_session() as s:
        rows = (
            await s.exec(
                text(
                    "SELECT COLUMN_NAME, DATA_TYPE FROM information_schema.COLUMNS "
                    "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'msg_notify' "
                    "AND COLUMN_NAME IN ('target_type','level','status')"
                )
            )
        ).all()
        assert len(rows) == 3
        for name, dtype in rows:
            assert dtype.lower() == "varchar", f"{name} 应为 varchar，实际 {dtype}"

        # IntEnum 列必须是整型
        int_rows = (
            await s.exec(
                text(
                    "SELECT COLUMN_NAME, DATA_TYPE FROM information_schema.COLUMNS "
                    "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'msg_dm_index' "
                    "AND COLUMN_NAME = 'msg_status'"
                )
            )
        ).all()
        assert int_rows and int_rows[0][1].lower() in ("int", "integer", "bigint")


async def test_int_enum_round_trip() -> None:
    """IntEnum 列：库里是整数 value，ORM 读回是枚举成员。"""
    await _cleanup()
    async with new_session() as s:
        session_row = DmSession(
            owner_mid=E_MID,
            talker_mid=E_TALKER,
            session_key=f"{E_MID}_{E_TALKER}",
            session_type=DmSessionTypeEnum.SINGLE,
            relation=DmRelationEnum.STRANGER,
            last_msg_ts=0,
        )
        index_row = DmMessageIndex(
            owner_mid=E_MID,
            talker_mid=E_TALKER,
            session_key=f"{E_MID}_{E_TALKER}",
            msgkey=E_MSGKEY,
            sender_uid=E_TALKER,
            msg_type=DmMsgTypeEnum.IMAGE,
            msg_status=DmMsgStatusEnum.RECALLED,
            msg_ts=0,
        )
        s.add(session_row)
        s.add(index_row)
        await s.commit()
        await s.refresh(session_row)
        await s.refresh(index_row)

        # 原生 SQL：IntEnum 存整数，同表的 StrEnum 存小写 value
        raw_sess = (
            await s.exec(
                text(
                    "SELECT session_type, relation FROM msg_dm_session "
                    f"WHERE id = {session_row.id}"
                )
            )
        ).one()
        assert raw_sess[0] == DmSessionTypeEnum.SINGLE.value == 1
        assert raw_sess[1] == "stranger"

        raw_idx = (
            await s.exec(
                text(
                    "SELECT msg_type, msg_status FROM msg_dm_index "
                    f"WHERE id = {index_row.id}"
                )
            )
        ).one()
        assert raw_idx[0] == "image"
        assert raw_idx[1] == DmMsgStatusEnum.RECALLED.value == 1

        # ORM 读回还原成枚举成员
        s.expunge_all()
        got_sess = (
            await s.exec(select(DmSession).where(DmSession.id == session_row.id))
        ).one()
        got_idx = (
            await s.exec(
                select(DmMessageIndex).where(DmMessageIndex.id == index_row.id)
            )
        ).one()
        assert got_sess.session_type is DmSessionTypeEnum.SINGLE
        assert got_sess.relation is DmRelationEnum.STRANGER
        assert got_idx.msg_type is DmMsgTypeEnum.IMAGE
        assert got_idx.msg_status is DmMsgStatusEnum.RECALLED

        # 按 IntEnum 成员过滤能命中
        hit = (
            await s.exec(
                select(DmMessageIndex).where(
                    DmMessageIndex.owner_mid == E_MID,
                    DmMessageIndex.msg_status == DmMsgStatusEnum.RECALLED,
                )
            )
        ).all()
        assert [h.id for h in hit] == [index_row.id]
    await _cleanup()


async def test_event_enum_round_trip_all_members() -> None:
    """遍历事件枚举全部成员，逐一验证写入 → 读回一致。"""
    await _cleanup()
    async with new_session() as s:
        created: list[tuple[int, EventTypeEnum, SourceTypeEnum]] = []
        for i, (et, st) in enumerate(
            [
                (e, s_)
                for e in EventTypeEnum
                for s_ in SourceTypeEnum
            ]
        ):
            row = EventMessage(
                mid=E_MID,
                event_type=et,
                source_type=st,
                source_id=f"src{i}",
                actor_mid=E_TALKER,
                dedup_key=f"e2e-enum-{i}-{E_MID}",
            )
            s.add(row)
            await s.commit()
            await s.refresh(row)
            created.append((row.id, et, st))

        s.expunge_all()
        for rid, et, st in created:
            got = (
                await s.exec(select(EventMessage).where(EventMessage.id == rid))
            ).one()
            assert got.event_type is et, f"event_type 往返不一致: {got.event_type} != {et}"
            assert got.source_type is st, f"source_type 往返不一致: {got.source_type} != {st}"

        # 库里字面量全部为小写 value
        raws = (
            await s.exec(
                text(
                    "SELECT DISTINCT event_type, source_type FROM msg_event "
                    f"WHERE mid = {E_MID}"
                )
            )
        ).all()
        valid_et = {e.value for e in EventTypeEnum}
        valid_st = {e.value for e in SourceTypeEnum}
        for et_raw, st_raw in raws:
            assert et_raw in valid_et, f"库中 event_type 字面量异常: {et_raw!r}"
            assert st_raw in valid_st, f"库中 source_type 字面量异常: {st_raw!r}"
    await _cleanup()


async def test_comment_enum_columns_varchar_and_int() -> None:
    """评论系统枚举列物理类型回归：StrEnum → VARCHAR，IntEnum → 整数。

    与全局约定一致（见 test_str_enum_column_is_varchar_not_native_enum）：
    一旦有人把 `str_enum_type` 误改回默认行为，这些列会退化成 MySQL 原生 ENUM，
    新增枚举值就要改表结构，且与接口层小写 value 对不上。这里锁死物理类型。
    """
    async with new_session() as s:
        # StrEnum 列应为 VARCHAR
        varchar_cols = (
            await s.exec(
                text(
                    "SELECT COLUMN_NAME, DATA_TYPE FROM information_schema.COLUMNS "
                    "WHERE TABLE_SCHEMA = DATABASE() "
                    "AND TABLE_NAME = 'msg_comment_index' "
                    "AND COLUMN_NAME IN ('type','state')"
                )
            )
        ).all()
        assert len(varchar_cols) == 2, "msg_comment_index 缺少 type/state 列"
        for name, dtype in varchar_cols:
            assert dtype.lower() == "varchar", f"{name} 应为 varchar，实际 {dtype}"

        sub_cols = (
            await s.exec(
                text(
                    "SELECT COLUMN_NAME, DATA_TYPE FROM information_schema.COLUMNS "
                    "WHERE TABLE_SCHEMA = DATABASE() "
                    "AND TABLE_NAME = 'msg_comment_subject' "
                    "AND COLUMN_NAME IN ('type','state')"
                )
            )
        ).all()
        assert all(d.lower() == "varchar" for _, d in sub_cols)

        at_cols = (
            await s.exec(
                text(
                    "SELECT COLUMN_NAME, DATA_TYPE FROM information_schema.COLUMNS "
                    "WHERE TABLE_SCHEMA = DATABASE() "
                    "AND TABLE_NAME = 'msg_comment_at' AND COLUMN_NAME = 'type'"
                )
            )
        ).all()
        assert at_cols and at_cols[0][1].lower() == "varchar"

        # IntEnum 列应为整数
        int_cols = (
            await s.exec(
                text(
                    "SELECT COLUMN_NAME, DATA_TYPE FROM information_schema.COLUMNS "
                    "WHERE TABLE_SCHEMA = DATABASE() "
                    "AND TABLE_NAME = 'msg_comment_action' AND COLUMN_NAME = 'action'"
                )
            )
        ).all()
        assert int_cols and int_cols[0][1].lower() in ("int", "integer", "bigint")
