"""RPC 真实 RabbitMQ 往返测试（集成测试，broker 不可达时自动 skip）。

与 `test_rpc_wiring.py` 的分工：
- `test_rpc_wiring.py`：**接线守卫**，纯内存，不需要任何 broker，跑得快，是 CI 的回归防线；
- 本文件：**端到端**，真的把消息发到 RabbitMQ 再等回包，验证 request/reply 全链路
  （含 Direct Reply-To、rpc_safe 回包、真实 GeoLite2 mmdb）。

运行方式（在 be-message-service 目录下）::

    # 1) 只跑快速测试（本文件在 broker 不可达时自动 skip）
    uv run pytest tests/ -v

    # 2) 只跑真实 broker 的集成测试（URL 需在导入 app 之前给出，见下方说明）
    RABBITMQ_URL='amqp://admin:114514@127.0.0.1:5672/?heartbeat=180' \
        uv run pytest tests/test_rpc_live_rabbitmq.py -v -s

    # 3) 按 marker 筛集成测试
    uv run pytest -m integration -v

broker URL 的解析顺序（`BROKER_URL`）：环境变量 `RABBITMQ_URL` > `app/.env` 的
`rabbitmq_url` > 内置默认（docker 内网服务名 `rabbitmq`）。注意本机 `app/.env` 配的是
`amqp://admin:114514@localhost:5672/`，即容器 RabbitMQ 的映射端口 —— 所以宿主机上直接
`pytest` 就会真的连上容器执行（不会 skip）；换到 broker 不可达的环境才自动 skip。

⚠️ 注意：
- `RABBITMQ_URL` 必须在 **pytest 启动前**设置：app 的 broker 在 import 期就用它建好了。
- 跑本文件会像服务正常启动那样，在目标 broker 上**声明 app 的全部队列**（幂等）。
  请只在开发 / 测试用的 broker 上跑。
"""

import json
import os
import socket
from urllib.parse import urlparse

import pytest
from faststream.rabbit import RabbitBroker

# 先加载 app.main，建立可用的导入顺序（避免 app.core.broker ↔ app.mq.router 的
# 既有循环导入），再按需 import app.mq.*。
import app.main  # noqa: F401
from app.core.broker import broker
from app.core.config import settings
from bili_common.rpc.base import geoip_rpc_routing_key_for
from bili_common.rpc.geoip import GeoIpRpcMethodName

GEOIP_RK = geoip_rpc_routing_key_for(GeoIpRpcMethodName.RESOLVE_IP_REGION)
REQUEST_TIMEOUT = 10.0

# 容器 RabbitMQ 的宿主机映射端口；也可用环境变量覆盖
BROKER_URL = os.environ.get("RABBITMQ_URL") or settings.rabbitmq_url


def _reachable(url: str) -> bool:
    """TCP 探一下 broker 是否可达（不做 AMQP 握手，够用来决定 skip）。"""
    parsed = urlparse(url)
    host, port = parsed.hostname, parsed.port or 5672
    if not host:
        return False
    try:
        with socket.create_connection((host, port), timeout=1.5):
            return True
    except OSError:
        return False


def _mmdb_available() -> bool:
    """真实 mmdb 是否在位（不在位时属地解析按弱依赖降级为空串）。"""
    from pathlib import Path

    mmdb_dir = Path(settings.geoip_mmdb_dir)
    return (mmdb_dir / "GeoLite2-City.mmdb").is_file()


pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not _reachable(BROKER_URL),
        reason=f"RabbitMQ 不可达（{BROKER_URL}），跳过真实 broker 集成测试",
    ),
]


async def _request(client: RabbitBroker, ip: str) -> dict:
    """发一次 geoip RPC 并解析回包（与 RPA-Browser 的 RpcClient 同一条路径）。"""
    resp = await client.request({"ip": ip}, queue=GEOIP_RK, timeout=REQUEST_TIMEOUT)
    return json.loads(resp.body.decode())


async def test_geoip_rpc_roundtrip_on_real_broker():
    """真实 broker 上跑三种 IP：公网 / 内网 / 空串，全部应 code=0 且结构正确。"""
    # 服务端：复用 app 自己的 broker（真实连接 + 声明队列 + 开始消费）
    await broker.start()
    # 客户端：独立连接，走 broker.request（Direct Reply-To）
    client = RabbitBroker(BROKER_URL)
    await client.start()
    try:
        cases = {
            "114.114.114.114": None,  # 公网：有 mmdb 时应有属地
            "192.168.1.10": "",  # 内网：恒为空串
            "": "",  # 空 IP：恒为空串
        }
        for ip, expected_region in cases.items():
            body = await _request(client, ip)

            # 关键：不是超时、不是 500（rpc_safe 兜底），而是正常业务信封
            assert body["code"] == 0, f"ip={ip!r} 回包异常: {body}"
            assert set(body["data"]) == {"region", "isp"}, body

            if expected_region is not None:
                assert body["data"] == {"region": "", "isp": ""}, f"ip={ip!r}: {body}"
            elif _mmdb_available():
                assert body["data"]["region"], f"ip={ip!r} 未解析出属地: {body}"
    finally:
        await client.stop()
        await broker.stop()


@pytest.mark.xfail(
    strict=False,
    reason=(
        "已知缺陷：参数校验（RequestValidationError）发生在 rpc_safe 之外，"
        "FastStream 不回包导致客户端超时；修好后会自动 XPASS"
    ),
)
async def test_rpc_error_is_returned_as_envelope_not_timeout():
    """参数校验失败也必须拿到回包（而不是干等到超时）—— RPC 契约的底线。

    ⚠️ 当前 **xfail**：FastAPI 兼容链路的参数校验（`RequestValidationError`）发生在
    `rpc_safe` 之外（`rpc_safe` 只包住 handler 本身），FastStream 在 handler 抛异常时
    不向 reply_to 回包，于是客户端只能干等到超时 —— 与 `rpc_safe` 想解决的正是同一个
    问题，只是校验阶段没被覆盖。修法（待办）：在 RPC 边界再加一层把校验异常也翻成
    `error_response` 的兜底（middleware / 自定义 parser，或让 FastAPI 集成层回包）。

    行为修好后本用例会变成 XPASS（strict=False 不报错），届时去掉 xfail 标记即可。
    """
    # 校验失败没有回包，客户端会一直等到超时；这里用短超时让失败快速暴露
    probe_timeout = 2.0

    await broker.start()
    client = RabbitBroker(BROKER_URL)
    await client.start()
    try:
        try:
            resp = await client.request(
                {"ip": {"nested": "not-a-string"}},
                queue=GEOIP_RK,
                timeout=probe_timeout,
            )
            body = json.loads(resp.body.decode())
        except Exception as e:  # noqa: BLE001 - 超时同样属于「没拿到信封」
            pytest.fail(f"参数非法时应回错误信封而不是异常/超时: {type(e).__name__}: {e}")
        assert body["code"] != 0, f"非法参数不应返回成功: {body}"
    finally:
        await client.stop()
        await broker.stop()
