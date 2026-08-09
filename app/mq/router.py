"""RabbitMQ FastAPI 路由（FastStream 集成入口）。

把所有 MQ 消费者集中到一个 `RabbitRouter`，作为 FastAPI 插件接入：

- **broker 生命周期由 router 自带 lifespan 管理**：FastAPI 在 `include_router`
  时会通过 `_merge_lifespan_context` 把 router 的 lifespan 合并进 app 的 lifespan。
  启动顺序为：app lifespan startup（连通性检查 / 迁移 / 分片预热）→ router
  lifespan startup（`broker.start()`）→ `after_startup` 钩子（启动定时任务）→
  yield（服务运行）→ `on_broker_shutdown` 钩子（停定时任务）→ `broker.stop()` →
  app lifespan teardown。
- **消费者分散在 `app/mq/consumers/` 下**，每个文件用 `@router.subscriber(...)`
  注册，由 `app/mq/consumers/__init__.py` 统一 import 触发副作用。
- **publisher / RPC 共用同一个 broker 实例**：`broker = router.broker`，
  避免 publisher 与 consumer 各起一个连接。
- **健康检查 / 连通性探测**也复用 `router.broker`。

注意：连通性预检、Alembic 迁移、分片预热等 app 级启动逻辑仍然留在
`app/main.py` 的 lifespan 里——它们必须早于 `broker.start()` 完成（router 的
lifespan 嵌套在 app lifespan 内部，靠 `_merge_lifespan_context` 保证顺序）。
"""

from faststream.rabbit.fastapi import RabbitRouter

from app.core.config import settings
from app.tasks import shutdown_scheduler, start_scheduler

# RabbitRouter 内部会创建一个 RabbitBroker 实例并自行管理其生命周期
# （start / stop 都由 router 的 lifespan 接管，main.py 不再手动调用）

router = RabbitRouter(settings.rabbitmq_url)

# 单一 broker 实例，供 publisher / RPC 服务端 / health 检查共用
broker = router.broker


# ==================== 定时任务钩子 ====================
# start_scheduler 必须在 broker 启动之后：定时任务里可能投递消息，
# 此时 broker 已就绪。after_startup 钩子在 _start_broker() 之后、yield 之前触发。
# 必须 async：FastStream 对 sync 钩子用 run_in_threadpool 在工作线程执行，
# 而 AsyncIOScheduler.start() 需在事件循环线程中调用 asyncio.get_running_loop()。
@router.after_startup
async def _start_scheduler_hook(_app) -> None:
    start_scheduler()


# shutdown_scheduler 必须在 broker.stop() 之前：避免调度器仍尝试投递到已关闭的
# 连接。on_broker_shutdown 钩子在 broker.stop() 之前触发。
@router.on_broker_shutdown
async def _stop_scheduler_hook(_app) -> None:
    shutdown_scheduler()


__all__ = ["router", "broker"]
