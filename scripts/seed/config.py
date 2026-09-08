"""seed 全局配置：HTTP 超时、请求并发上限与各类业务常量。

超时取值背景见下方注释：对外短雪花 ID 为「分钟级」，服务端会在锁外 sleep 到下一分钟
（最长 60s），客户端超时必须 > 60s，否则会在分钟边界误报 ReadTimeout。
"""
import os

# ---------------------------------------------------------------------------
# 超时配置（可用环境变量覆盖，默认值已覆盖雪花 ID 分钟级等待）
# ---------------------------------------------------------------------------
# 背景：对外短雪花 ID（moment_id 等）位布局为「分钟时间戳 31bit + worker 4bit
# + 序列号 4bit」，每个 worker 每分钟最多生成 16 个。灌数速率超过该上限时，
# 服务端会在锁外 sleep 到下一分钟（最长 60s）。客户端超时若 < 60s，会在分钟
# 边界误报 ReadTimeout。因此：
# - SEED_HTTP_TIMEOUT：httpx 单次传输超时，默认 90s（= 跨分钟等待 + 处理余量）；
# - SEED_REQ_TIMEOUT：_req 单次请求总超时（asyncio.wait_for），默认 120s；
# - SEED_REQ_CONCURRENCY：客户端同时在途 HTTP 请求上限。必须压在 be-message 的
#   MySQL 连接池（pool_size + max_overflow，运行时常见 20+30=50）以内，否则服务端
#   会出现 QueuePool 耗尽 → TimeoutError → 500（见 logs/message-service.log）。
#   注意：单个动态任务内部还会 fan-out 出大量点赞/评论/浏览子请求，每个都是一次
#   独立 HTTP（独立占用一个服务端 DB 连接），所以仅限制「任务数」(sem) 不够，
#   必须在「每次请求」层面加全局信号量。弱事件上报（@ 通知）也会额外占用连接，
#   故留出余量，默认取 20（≤ 服务端 pool_size）。
_SEED_HTTP_TIMEOUT = float(os.environ.get("SEED_HTTP_TIMEOUT", "90"))
_SEED_REQ_TIMEOUT = float(os.environ.get("SEED_REQ_TIMEOUT", "120"))
_SEED_REQ_CONCURRENCY = int(os.environ.get("SEED_REQ_CONCURRENCY", "20"))


# 举报原因：与 ReportReasonEnum / MomentReportReasonEnum 对齐（1-6）
_REPORT_REASON_TYPE = 3  # 人身攻击
_BAN_SERVICES = ["comment"]

#: 混合资源池容量：评论/at/点赞在「动态 + lottery」混合池上进行，
#: 动态取前 10 个（原行为），lottery 取 2 个（通用资源链路覆盖够用即可，
#: 多了会挤占动态的评论额度且放大 crawler RPC 校验耗时）
_DYN_POOL_SIZE = 10
_LOTTERY_POOL_SIZE = 2

#: 评论置顶抽样概率（真随机）：置顶是「覆盖验证」型动作，无需对每个资源都测。
#: 阶段一 seed_comment 与大数据灌数 _seed_bulk_comment_suite 均对每个资源做置顶，
#: 大数据灌数 count 大时会产生上千次无意义置顶；这里按概率抽样（默认 10%）触发，
#: 既保覆盖又显著降量。0~1 之间，可调。
_TOP_COMMENT_PROB = float(os.environ.get("SEED_TOP_PROB", "0.1"))
