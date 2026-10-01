"""「IP 属地解析」RPC 服务端（be-message 为服务端）。

其它系统（RPA-Browser 等）经 RabbitMQ 按 `message.geoip.rpc.resolve_ip_region`
同步调用本模块，把客户端 IP 解析成可读属地（形如「浙江 杭州」），
用于「谁在看」列表与直播流日志。

为什么由本服务提供：

- **mmdb 单一来源**：GeoLite2 库（含 66MB 的 City 库）与下载 / 更新流程都只在本服务
  （`docker_vol/geoip/mmdb`，见 `scripts/download_geoip_mmdb.py`），
  其它服务不必各存一份、各推一次更新；
- **口径一致**：与评论 / 动态的 `lbsPoi` 共用同一段解析（`geo_ip.lookup`），
  不会出现「动态显示浙江、观看者显示杭州」。

一次调用同时返回**属地**（GeoLite2-City）与**运营商 / ISP**（GeoLite2-ASN，
ASN 组织名如「中国电信」）—— 调用方（RPA 观看者列表）本来就两项都要显示，
分两次 RPC 没有意义。

契约（方法名 / 请求 / 响应）统一来自 `bili_common.rpc.geoip`；
路由键前缀 `message.geoip.rpc.<method_name>`（见 GEOIP_RPC_ROUTING_KEY_PREFIX）。
本模块只需被 main.py import 一次即可完成 RPC 注册（FastStream 全局 broker 单例）。

⚠️ 注册必须用 `router.subscriber`（不能用 `broker.subscriber`）：本服务的 broker
来自 FastAPI 集成的 `RabbitRouter`，其 FD 配置走 FastAPI 的 `get_dependent`，
只有 `router.subscriber` 会挂上「FastAPI 兼容装饰器」先把消息体 decode 再注入 handler。
用 `broker.subscriber` 时该装饰器缺失，handler 会直接收到原始 `RabbitMessage`
（典型报错 `'RabbitMessage' object has no attribute 'xxx'`）。

降级：`lookup` 对「空 IP / 内网 / 回环 / 库缺失 / 未命中」会用 `poi="未知"`、`isp=None` 兜底，
这里统一转成**空串**再回包 —— 「未知」是展示文案，由调用方决定怎么显示。
"""

from faststream.rabbit import RabbitQueue

from bili_common.models.response import StandardResponse, success_response
from bili_common.rpc.base import geoip_rpc_routing_key_for
from bili_common.rpc.geoip import (
    GeoIpRpcMethodName,
    ResolveIpRegionParams,
    ResolveIpRegionResult,
)
from bili_common.rpc.safe import rpc_safe

from app.core.broker import message_exchange
from app.mq.router import router
from app.services.infrastructure.geo_ip import lookup


@router.subscriber(
    queue=RabbitQueue(
        geoip_rpc_routing_key_for(GeoIpRpcMethodName.RESOLVE_IP_REGION),
        routing_key=geoip_rpc_routing_key_for(GeoIpRpcMethodName.RESOLVE_IP_REGION),
        durable=True,
    ),
    exchange=message_exchange,
)
@rpc_safe
async def rpc_resolve_ip_region(params: ResolveIpRegionParams) -> StandardResponse:
    """按 IP 解析属地 + 运营商（resolve_ip_region）。

    一次调用同时返回属地（City 库）与运营商 / ISP（ASN 库），
    避免调用方为两项信息发两次 RPC。

    返回空串表示「没有可展示的值」（空 IP / 内网 / 未命中 / 库缺失），
    调用方据此回退为「未知属地」「未知运营商」；因此这里**不区分**
    「解析失败」与「无值」，调用方也不需要重试。
    """
    result = lookup(params.ip)
    # lookup 内部对失败场景用 poi="未知" 兜底、isp=None；在 RPC 边界统一归一成空串
    region = result.poi or ""
    if region == "未知":
        region = ""
    return success_response(
        data=ResolveIpRegionResult(region=region, isp=result.isp or "")
    )


__all__ = ["rpc_resolve_ip_region"]
