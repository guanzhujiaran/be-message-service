"""message-service 入口。

组合方式：FastAPI 负责对外暴露 REST 接口 / 健康检查 / OpenAPI；
FastStream(RabbitMQ) 作为「FastAPI 插件」通过 lifespan 接入——
应用启动时连接 broker 并开始消费 message 队列，关闭时断开。

这样既保留了「消费 RabbitMQ 并由 message-service 统一分发」的能力，
又能用标准 FastAPI 路由（/api/v1/message/...）对外提供 REST 接口，
/openapi.json 也会自动包含这些接口，供前端 hey-api 生成 SDK。
"""

from contextlib import asynccontextmanager

from fastapi import FastAPI, Response
from faststream import AckPolicy
from faststream.rabbit import RabbitMessage

from app.api.message import router as message_router
from app.broker import broker, message_exchange, message_queue
from app.config import settings
from app.consumer import handle_message
from app.models import PushMessage


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


@broker.subscriber(queue=message_queue, exchange=message_exchange, ack_policy=AckPolicy.MANUAL)
async def consume_message(message: PushMessage, msg: RabbitMessage) -> None:
    await handle_message(message, msg)


@asynccontextmanager
async def lifespan(app: FastAPI):
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
app.include_router(message_router)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=settings.http_port)
