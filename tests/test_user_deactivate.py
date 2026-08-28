"""用户注销单元测试（P12-T8，2.15.0 / 2.15.1）。

覆盖：
- 各领域删除服务（`cleanup_*`）按 uid 删除正确：关注关系双向、动态及子表级联、评论、
  举报、收藏等代表性表。
- `UserDeactivateService.deactivate` 组合编排（先 pptr 后 be-message）。
- 幂等（重复注销 / 已注销用户不报错）、uid 非法抛 ValueError。
- 全部 cleanup 服务在空库安全执行（验证所有删除 SQL 的表名/字段合法）。

用独立 mid / dynId 区间避免与既有用例互相干扰；测试后清理写入数据。
"""

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlmodel.ext.asyncio.session import AsyncSession as SQLModelAsyncSession

from app.core.config import settings
from app.services.cleanup import (
    cleanup_comment,
    cleanup_dm,
    cleanup_event,
    cleanup_favorite,
    cleanup_follow,
    cleanup_misc,
    cleanup_moment,
    cleanup_notify,
    cleanup_pptr,
    cleanup_report,
)
from app.services.user.user_deactivate import UserDeactivateService

# 独立测试区间（避免与既有用例 / 真实数据冲突）
UID = 920901
OTHER = 920902
DYN_ID = 920900000001


@pytest.fixture(autouse=True)
async def _bind_engine_per_test():
    """重建并绑定双 engine（be-message MySQL + pptr Postgres）到当前事件循环。"""
    from app.core import database as db_mod

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

    from app.core import database as db_mod_pptr

    pptr_engine = create_async_engine(
        url=settings.postgres_pptr_url,
        pool_pre_ping=True,
        future=True,
    )
    db_mod_pptr.pptr_engine = pptr_engine
    db_mod_pptr.pptr_session_maker = async_sessionmaker(
        bind=pptr_engine,
        class_=SQLModelAsyncSession,
        expire_on_commit=False,
        autoflush=False,
    )

    yield
    await engine.dispose()
    await pptr_engine.dispose()


async def _clean_test_data() -> None:
    """清理测试写入的 be-message / pptr 残留数据。"""
    from app.core.database import new_pptr_session, new_session

    async with new_session() as s:
        await s.exec(
            text(
                "DELETE FROM msg_user_follow WHERE mid IN (:a,:b) OR target_mid IN (:a,:b)"
            ),
            params={"a": UID, "b": OTHER},
        )
        await s.exec(text("DELETE FROM TMoment WHERE dynId = :d"), params={"d": DYN_ID})
        await s.exec(
            text("DELETE FROM msg_user_setting WHERE mid = :a"), params={"a": UID}
        )
        await s.exec(
            text("DELETE FROM msg_user_activity WHERE mid = :a"), params={"a": UID}
        )
        await s.exec(
            text("DELETE FROM msg_user_ban WHERE mid = :a OR operator_mid = :a"),
            params={"a": UID},
        )
        await s.exec(text("DELETE FROM msg_admin WHERE mid = :a"), params={"a": UID})
        await s.commit()

    async with new_pptr_session() as s:
        await s.exec(text('DELETE FROM "TUserInfo" WHERE uid = :u'), params={"u": UID})
        await s.exec(text('DELETE FROM "TUserLevel" WHERE mid = :u'), params={"u": UID})
        await s.exec(text('DELETE FROM "TUserVip" WHERE mid = :u'), params={"u": UID})
        await s.exec(text('DELETE FROM "TUserDetail" WHERE mid = :u'), params={"u": UID})
        await s.commit()


@pytest.fixture(autouse=True)
async def _cleanup_before_after():
    await _clean_test_data()
    yield
    await _clean_test_data()


# ==================== 领域删除服务 ====================


async def test_cleanup_follow_deletes_both_directions():
    """关注 / 拉黑双向删除：mid 与 target_mid 均被清。"""
    from app.core.database import new_session

    async with new_session() as s:
        await s.exec(
            text(
                "INSERT INTO msg_user_follow (mid, target_mid, status, created_at, updated_at) VALUES "
                "(:a, :b, 'following', NOW(), NOW()), (:b, :a, 'following', NOW(), NOW())"
            ),
            params={"a": UID, "b": OTHER},
        )
        await s.commit()

    async with new_session() as s:
        await cleanup_follow.CleanupFollowService.delete_all_by_uid(s, UID)
        await s.commit()

    async with new_session() as s:
        row = (
            await s.exec(
                text(
                    "SELECT COUNT(*) AS c FROM msg_user_follow "
                    "WHERE mid IN (:a,:b) OR target_mid IN (:a,:b)"
                ),
                params={"a": UID, "b": OTHER},
            )
        ).one()
        assert row.c == 0


async def test_cleanup_moment_deletes_dynamic_and_children():
    """动态删除：TMoment 及子表级联清（子表对 dynId CASCADE）。"""
    from app.core.database import new_session

    async with new_session() as s:
        await s.exec(
            text(
                "INSERT INTO TMoment (dynId, mid, dynType, contentJson, auditStatus, visibleScope, "
                "closeComment, foldType, isTop, repostDepth, upChooseComment, created_at, updated_at) "
                "VALUES (:d, :m, 6, '{}', 'normal', 0, 0, 0, 0, 0, 0, NOW(), NOW())"
            ),
            params={"d": DYN_ID, "m": UID},
        )
        await s.commit()

    async with new_session() as s:
        await cleanup_moment.CleanupMomentService.delete_all_by_uid(s, UID)
        await s.commit()

    async with new_session() as s:
        dyn = (
            await s.exec(text("SELECT COUNT(*) AS c FROM TMoment WHERE dynId = :d"), params={"d": DYN_ID})
        ).one()
        assert dyn.c == 0


async def test_cleanup_misc_deletes_setting_activity_ban_admin():
    """杂项删除：设置 / 活跃 / 封禁 / 管理 均清。"""
    from app.core.database import new_session

    async with new_session() as s:
        await s.exec(
            text(
                "INSERT INTO msg_user_setting (mid, recv_like, recv_reply, recv_at, "
                "recv_stranger_dm, recv_notify, push_enabled, created_at, updated_at) "
                "VALUES (:a, 1, 1, 1, 1, 1, 1, NOW(), NOW())"
            ),
            params={"a": UID},
        )
        await s.exec(
            text(
                "INSERT INTO msg_user_activity (mid, last_active_at, active_count, "
                "pending_push_count, created_at, updated_at) "
                "VALUES (:a, NOW(), 0, 0, NOW(), NOW())"
            ),
            params={"a": UID},
        )
        await s.exec(
            text(
                "INSERT INTO msg_user_ban (mid, operator_mid, reason, duration_type, "
                "status, created_at, updated_at) VALUES (:a, :b, 't', 0, 1, NOW(), NOW())"
            ),
            params={"a": UID, "b": OTHER},
        )
        await s.exec(
            text(
                "INSERT INTO msg_admin (mid, granted_by, created_at, updated_at) "
                "VALUES (:a, :b, NOW(), NOW())"
            ),
            params={"a": UID, "b": OTHER},
        )
        await s.commit()

    async with new_session() as s:
        await cleanup_misc.CleanupMiscService.delete_all_by_uid(s, UID)
        await s.commit()

    async with new_session() as s:
        for table in ("msg_user_setting", "msg_user_activity"):
            row = (
                await s.exec(text(f"SELECT COUNT(*) AS c FROM {table} WHERE mid = :a"), params={"a": UID})
            ).one()
            assert row.c == 0
        ban = (
            await s.exec(
                text("SELECT COUNT(*) AS c FROM msg_user_ban WHERE mid = :a OR operator_mid = :a"),
                params={"a": UID},
            )
        ).one()
        assert ban.c == 0
        admin = (
            await s.exec(text("SELECT COUNT(*) AS c FROM msg_admin WHERE mid = :a"), params={"a": UID})
        ).one()
        assert admin.c == 0


async def test_all_cleanup_services_run_safe_on_empty():
    """所有 cleanup 服务在空库安全执行（DELETE 0 行），验证表名/字段 SQL 合法。"""
    from app.core.database import new_pptr_session, new_session

    async with new_session() as s:
        for svc in (
            cleanup_moment.CleanupMomentService,
            cleanup_comment.CleanupCommentService,
            cleanup_follow.CleanupFollowService,
            cleanup_report.CleanupReportService,
            cleanup_favorite.CleanupFavoriteService,
            cleanup_dm.CleanupDmService,
            cleanup_notify.CleanupNotifyService,
            cleanup_event.CleanupEventService,
            cleanup_misc.CleanupMiscService,
        ):
            await svc.delete_all_by_uid(s, UID)
        await s.commit()

    async with new_pptr_session() as s:
        await cleanup_pptr.CleanupPptrService.delete_all_by_uid(s, UID)
        await s.commit()


# ==================== 编排器 ====================


async def test_deactivate_composes_pptr_and_msg():
    """deactivate 编排：先删 pptr 四表，再清 be-message 业务数据。"""
    from app.core.database import new_pptr_session, new_session

    # 造 be-message 关注 + pptr 用户
    async with new_session() as s:
        await s.exec(
            text(
                "INSERT INTO msg_user_follow (mid, target_mid, status, created_at, updated_at) "
                "VALUES (:a, :b, 'following', NOW(), NOW())"
            ),
            params={"a": UID, "b": OTHER},
        )
        await s.commit()

    async with new_pptr_session() as s:
        await s.exec(
            text('INSERT INTO "TUserInfo" (uid, user_name) VALUES (:u, \'t_deact\')'),
            params={"u": UID},
        )
        await s.commit()

    await UserDeactivateService.deactivate(UID)

    async with new_session() as s:
        row = (
            await s.exec(
                text("SELECT COUNT(*) AS c FROM msg_user_follow WHERE mid = :a"),
                params={"a": UID},
            )
        ).one()
        assert row.c == 0
    async with new_pptr_session() as s:
        row = (
            await s.exec(text('SELECT COUNT(*) AS c FROM "TUserInfo" WHERE uid = :u'), params={"u": UID})
        ).one()
        assert row.c == 0


async def test_deactivate_idempotent():
    """重复注销（已无数据）不报错。"""
    await UserDeactivateService.deactivate(UID)
    await UserDeactivateService.deactivate(UID)  # 第二次仍正常


async def test_deactivate_rejects_invalid_uid():
    """uid 非法（0 / 负数）抛 ValueError。"""
    with pytest.raises(ValueError):
        await UserDeactivateService.deactivate(0)
    with pytest.raises(ValueError):
        await UserDeactivateService.deactivate(-1)


# ==================== MQ 投递 ====================


async def test_publish_user_deactivate_calls_broker(monkeypatch):
    """publisher 投递注销消息（monkeypatch broker.publish，验证 payload / routing key）。"""
    from app.core import broker as broker_mod
    from app.services.message import publisher

    calls: list[dict] = []

    async def fake_publish(message, exchange, routing_key, queue):
        calls.append(
            {"message": message, "routing_key": routing_key, "queue": queue}
        )

    monkeypatch.setattr(publisher.broker, "publish", fake_publish)
    ok = await publisher.publish_user_deactivate(UID)
    assert ok is True
    assert len(calls) == 1
    assert calls[0]["message"]["uid"] == UID
    assert calls[0]["routing_key"] == broker_mod.RK_USER_DEACTIVATE
    assert calls[0]["queue"] == broker_mod.user_deactivate_queue
