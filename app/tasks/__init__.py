"""后台定时任务（APScheduler）。

承担三件「不适合放在请求链路里」的事：

1. **非活跃用户的批量聚合推送**：把一段时间内堆积的提醒合并成一条再推，
   既降低对沉默用户的打扰，也把小设备上的推送压力削成固定节拍。
2. **系统通知的定时投递**：扫描「已到发布时间但尚未投递」的通知并推送，
   靠 `dispatched` 标记保证任务重入不会重复推送。
3. **兜底补偿**：私信正文写入失败的死信重试、跨月时的分片预热。
"""

from app.tasks.scheduler import scheduler, shutdown_scheduler, start_scheduler

__all__ = ["scheduler", "start_scheduler", "shutdown_scheduler"]
