"""message-service 入口。

组合方式：FastAPI 负责对外暴露 REST 接口 / 健康检查 / OpenAPI；
FastStream(RabbitMQ) 作为「FastAPI 插件」通过 lifespan 接入——
应用启动时连接 broker 并开始消费各消息队列，关闭时断开。

消息系统包含四个功能模块，均挂在 `/api/v1/message/` 之下：

| 模块     | 前缀                        | 说明                                       |
| -------- | --------------------------- | ------------------------------------------ |
| 系统通知 | `/api/v1/message/notify`    | 管理员发布、定时拉取（游标去重）、已读管理 |
| 事件提醒 | `/api/v1/message/event`     | 点赞 / 回复 / @，按来源实体聚合展示        |
| 私信     | `/api/v1/message/dm`        | 单聊写扩散，正文月度分库分表 + 异步落库    |
| 消息设置 | `/api/v1/message/setting`   | 各类提醒开关、陌生人私信、免打扰时段       |
| 推送     | `/api/v1/message/push`      | 外部渠道推送（历史能力）                   |
| 聚合     | `/api/v1/message/msg_feed`  | 跨模块未读汇总、活跃心跳                   |

启动顺序：依赖自检（MySQL / RabbitMQ）→ Alembic 迁移 → 当月分片预热
→ 连接 broker 开始消费 → 启动后台定时任务。
"""

from contextlib import asynccontextmanager

import httpx
from bili_common.exceptions import register_exception_handlers
from fastapi import FastAPI, Response
from faststream.rabbit import RabbitBroker
from loguru import logger

from app.api.ban import router as ban_router
from app.api.comment import router as comment_router
from app.api.comment_admin import router as comment_admin_router
from app.api.dm import router as dm_router
from app.api.dm_admin import router as dm_admin_router
from app.api.event import router as event_router
from app.api.follow import router as follow_router
from app.api.message_admin import router as message_admin_router
from app.api.msg_feed import router as msg_feed_router
from app.api.notify import router as notify_router
from app.api.pptr_user_gateway import router as pptr_user_gateway_router
from app.api.push import router as push_router
from app.api.setting import router as setting_router
from app.api.user import router as user_router
from app.core.broker import broker
from app.core.config import settings
from app.core.database import ensure_database, test_pptr_connection
from app.core.database import test_connection as test_mysql_connection
from app.core.migration import run_alembic_pptr_upgrade, run_alembic_upgrade
from app.core.sharding import ensure_current_month_shards
from app.mq import rpc_pptr_user  # noqa: F401
from app.mq.consumers import comment, dm, push  # noqa: F401
from app.mq.router import router as mq_router


async def test_service_connectivity():
    """检查 message-service 依赖的关键服务连通性，失败则拒绝启动。

    关键服务（critical=True，未启动直接 SystemExit 终止启动）：
    - RabbitMQ：消息队列，未连接则无法消费推送请求与私信正文落库。
    - MySQL：消息系统的唯一存储（本项目不使用 Redis），不通则整个系统不可用。

    非关键服务（仅告警，不阻断启动）：
    - PushMe / PushPlus 默认端点：外部第三方推送渠道，且具体渠道可由
      消息内 config 覆盖，单个端点故障仅影响对应渠道。
    """
    failed_critical = []

    # ---- RabbitMQ（关键）----
    try:
        test_broker = RabbitBroker(settings.rabbitmq_url)
        await test_broker.connect()
        # 兼容不同 faststream 版本的断开方法（旧版为 close，新版为 disconnect）
        for _meth in ("disconnect", "close"):
            if hasattr(test_broker, _meth):
                await getattr(test_broker, _meth)()
                break
        logger.info(f"RabbitMQ 连接成功 ({settings.rabbitmq_url})")
    except Exception as e:
        failed_critical.append(f"RabbitMQ ({settings.rabbitmq_url})")
        logger.critical(f"RabbitMQ 连接失败: {e}")

    # ---- MySQL（关键）----
    # 先确保主库存在（首次部署时 BiliMessageDB 尚未创建），再探测连通性
    await ensure_database()
    if await test_mysql_connection():
        logger.info("MySQL 连接成功")
    else:
        failed_critical.append("MySQL (mysql_message_url)")
        logger.critical("MySQL 连接失败，请检查 MYSQL_MESSAGE_URL 配置")

    # ---- 外部推送渠道端点（非关键）----
    endpoints = [
        ("PushMe", settings.pushme_url),
        ("PushPlus", settings.pushplus_url),
    ]
    for name, url in endpoints:
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(url, timeout=5.0, follow_redirects=True)
            logger.info(f"{name} 端点连通性正常 ({url}, HTTP {resp.status_code})")
        except Exception as e:
            logger.warning(
                f"{name} 端点 ({url}) 连通性检查失败（非关键，不阻断启动）: {e}"
            )

    # ---- pptr Postgres（只读，非关键）----
    # 用户展示信息 / @ 搜索统一直连 pptr 只读库；不可达时服务降级为空用户数据，
    # 不影响评论 / 私信核心链路，仅告警不阻断启动。
    if await test_pptr_connection():
        logger.info("pptr Postgres 只读连接成功")
    else:
        logger.warning(
            "pptr Postgres 只读连接失败（非关键，用户展示信息将降级为空，不阻断启动）"
        )

    if failed_critical:
        logger.critical("=" * 60)
        logger.critical(
            f"以下关键服务未启动，禁止 message-service 启动: {len(failed_critical)} 个"
        )
        for s in failed_critical:
            logger.critical(f"  🚫 {s}")
        logger.critical("=" * 60)
        raise SystemExit(
            f"关键依赖服务未启动，拒绝启动 message-service: {', '.join(failed_critical)}"
        )


# ==================== MQ 消费者注册 ====================
# 消费者注册胶水代码已迁出至 app/mq/consumers/，由顶部的
# `from app.mq.consumers import comment, dm, push` 触发副作用注册。
# broker.start() / broker.stop() / start_scheduler() / shutdown_scheduler()
# 不再在此手动调用——全部由 mq_router 的 lifespan 接管（与 app lifespan 合并）：
#   1. app lifespan startup：连通性检查 / 迁移 / 分片预热（必须早于 broker.start）
#   2. router lifespan startup：broker.start() + after_startup 钩子（启动定时任务）
#   3. yield：服务运行
#   4. router lifespan teardown：on_broker_shutdown 钩子（停定时任务）+ broker.stop()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 1. 启动前检查依赖服务连通性，关键服务未启动则拒绝启动
    await test_service_connectivity()

    # 2. 主库 Schema 拉齐到最新版本（月度内容分库不由 Alembic 管理）
    if settings.alembic_auto_migrate:
        await run_alembic_upgrade()
        # pptr Postgres 用户库单独走 alembic_pptr 分支，避免主从库迁移互相阻塞
        await run_alembic_pptr_upgrade()

    # 3. 预热当月私信内容分片，首条私信写入无需承担建表 DDL 耗时
    try:
        await ensure_current_month_shards()
    except Exception as e:  # noqa: BLE001
        logger.error(f"预热私信内容分片失败（不阻断启动，写入时会懒创建）: {e}")

    # broker 启动 / 定时任务启动由 mq_router 的 lifespan 在本 lifespan 之外、
    # 嵌套于其内部接管（FastAPI 的 _merge_lifespan_context 保证顺序）。
    yield


app = FastAPI(
    title="message-service",
    description=(
        "统一消息系统微服务（FastStream 消费者 + FastAPI REST 接口）。"
        "包含系统通知、事件提醒、私信、消息设置四大模块，以及历史的外部渠道推送能力。"
    ),
    lifespan=lifespan,
)


# ==================== 统一异常响应格式 ====================
# 所有异常（认证 / 权限 / 参数校验等）统一包装为 {code, msg, data} 形式，
# 与业务接口一致，避免 FastAPI 默认的 {"detail": ...} 破坏前端契约。
# 未登录等认证异常使用 B 站官方约定业务码 -101，HTTP 状态码恒为 200
# （详见 docs/response-code-design.md）。统一由 bili_common 处理。
register_exception_handlers(app)
app.include_router(mq_router)


@app.get(
    "/health",
    status_code=204,
    responses={503: {"description": "broker 未连接"}},
    summary="健康检查",
)
async def health() -> Response:
    """访问返回 204 即代表服务存活且 RabbitMQ broker 连接正常。"""
    try:
        ok = await broker.ping(5.0)
    except Exception:  # noqa: BLE001
        ok = False
    return Response(status_code=204 if ok else 503)


app.include_router(msg_feed_router)
app.include_router(comment_router)
app.include_router(comment_admin_router)
app.include_router(notify_router)
app.include_router(event_router)
app.include_router(dm_router)
app.include_router(dm_admin_router)
app.include_router(message_admin_router)
app.include_router(ban_router)
app.include_router(follow_router)
app.include_router(setting_router)
app.include_router(user_router)
app.include_router(push_router)
app.include_router(pptr_user_gateway_router)

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=settings.http_port)
