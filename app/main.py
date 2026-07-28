"""message-service 入口。

组合方式：FastAPI 负责对外暴露 REST 接口 / 健康检查 / OpenAPI；
FastStream(RabbitMQ) 作为「FastAPI 插件」通过 lifespan 接入——
应用启动时连接 broker 并开始消费 message 队列，关闭时断开。

这样既保留了「消费 RabbitMQ 并由 message-service 统一分发」的能力，
又能用标准 FastAPI 路由（/api/v1/message/...）对外提供 REST 接口，
/openapi.json 也会自动包含这些接口，供前端 hey-api 生成 SDK。
"""

from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Response
from faststream import AckPolicy
from faststream.rabbit import RabbitBroker, RabbitMessage
from loguru import logger

from app.api.msg_feed import router as msg_feed_router
from app.api.push import router as push_router
from app.consumers.push import handle_message
from app.core.broker import broker, message_exchange, message_queue
from app.core.config import settings
from app.models import PushMessagePayload


import logging

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[
        logging.StreamHandler(),  # Console logs
        logging.FileHandler("app.log"),  # File logs
    ],
)


async def test_service_connectivity():
    """检查 message-service 依赖的关键服务连通性，失败则拒绝启动。

    关键服务（critical=True，未启动直接 SystemExit 终止启动）：
    - RabbitMQ：消息队列，未连接则无法消费推送请求。

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
                f"{name} 端点 ({url}) 连通性检查失败（非关键，不阻断启动）: {e}")

    if failed_critical:
        logger.critical("=" * 60)
        logger.critical(
            f"以下关键服务未启动，禁止 message-service 启动: {len(failed_critical)} 个")
        for s in failed_critical:
            logger.critical(f"  🚫 {s}")
        logger.critical("=" * 60)
        raise SystemExit(
            f"关键依赖服务未启动，拒绝启动 message-service: {', '.join(failed_critical)}")


@broker.subscriber(queue=message_queue, exchange=message_exchange, ack_policy=AckPolicy.MANUAL)
async def consume_message(message: PushMessagePayload, msg: RabbitMessage) -> None:
    await handle_message(message, msg)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 启动前检查依赖服务连通性，关键服务未启动则拒绝启动
    await test_service_connectivity()
    # 以 FastAPI 插件方式接入 FastStream：启动即连接 broker 并开始消费 message 队列
    await broker.start()
    yield
    await broker.stop()


app = FastAPI(
    title="message-service",
    description="统一消息系统微服务（FastStream 消费者 + FastAPI REST 接口）；当前包含「推送」模块，后续将加入评论 / 对话 / 私信等",
    lifespan=lifespan,
)


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


# 标准 REST 接口（/api/v1/message/...）
app.include_router(msg_feed_router)

# 推送接口（/api/v1/push/...）
app.include_router(push_router)

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=settings.http_port)
