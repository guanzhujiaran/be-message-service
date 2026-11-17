"""管理端：be-crawler 后台爬虫服务（调度任务）启停。

对应 crawler 侧两个控制面接口：

- `GET  /api/v1/background_service/GlobalScheduler/Status`
  读取全局调度器里已注册的任务，用于判断某服务当前是否处于「已排期」状态；
- `POST /api/v1/background_service/BackgroundService/{Start,Stop,Restart}`
  `background_service_name` 走 query，取值同 crawler 的 `BackgroundServiceName`。

crawler 这些接口无鉴权且网关刻意不转发，因此由本服务用 `AdminUser` 鉴权后内网代理。
"""

from enum import StrEnum

from sqlmodel import Field, SQLModel

__all__ = [
    "CrawlerScheduledService",
    "CrawlerServiceAction",
    "CrawlerServiceOperateReq",
    "CrawlerServiceOperateResp",
    "CrawlerServiceStatusResp",
]


class CrawlerServiceAction(StrEnum):
    """对后台服务执行的启停动作"""

    START = "start"
    STOP = "stop"
    RESTART = "restart"


class CrawlerServiceOperateReq(SQLModel):
    """启停请求：服务名取值同 crawler 的 `BackgroundServiceName`"""

    service_name: str = Field(description="后台服务名，取值同 BackgroundServiceName")


class CrawlerScheduledService(SQLModel):
    """一个已注册在全局调度器里的后台服务"""

    service_name: str = Field(description="后台服务名")
    next_run_time: float | None = Field(
        default=None, description="下次触发时间（unix 秒）；null 表示未排期 / 已暂停"
    )


class CrawlerServiceStatusResp(SQLModel):
    """后台服务调度状态：只返回已注册的服务，未出现即为「已停止」"""

    scheduled: list[CrawlerScheduledService] = Field(
        default_factory=list, description="当前已注册在全局调度器中的后台服务"
    )


class CrawlerServiceOperateResp(SQLModel):
    """启停操作结果"""

    service_name: str = Field(description="后台服务名")
    action: CrawlerServiceAction = Field(description="执行的动作")
    msg: str = Field(description="crawler 返回的结果描述")
