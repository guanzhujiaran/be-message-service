"""MQ 消费者注册入口。

本包内的每个子模块都用 `@router.subscriber(...)` 装饰器注册消费者，
只需被 import 一次即完成注册（副作用驱动）。`app/main.py` 通过
`from app.mq.consumers import push, dm, comment  # noqa: F401` 触发注册。

各 handler 的实际处理逻辑位于 `app/consumers/` 下，本目录只负责
「把 handler 绑定到队列」的注册胶水。
"""

from app.mq.consumers import comment, dm, push  # noqa: F401

__all__ = ["push", "dm", "comment"]
