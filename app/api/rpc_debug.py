"""管理端「RPC 调试」接口（root 专用）。

用途：在浏览器里选一个契约方法、贴 JSON 参数，发起**真实** RPC 往返并回显完整信封，
替代「FastStream /asyncapi 的 Try it out（对自定义 exchange 必 404）」与
「RabbitMQ 管理台手填 reply_to」这两种别扭的调试方式。

安全边界（见计划书 §5.22）：
- 仅 root（`RootUser` 依赖）；
- 只能调用 `bili_common.rpc.*` 契约表登记过的方法，路由键不接受外部输入；
- 参数在 HTTP 边界用契约 params 模型严格校验，非法直接 400，坏消息不进 MQ。
"""

from fastapi import APIRouter
from pydantic import BaseModel, Field

from app.dependencies import RootUser
from app.models import StandardResponse
from app.services.admin.rpc_debug import (
    DEFAULT_TIMEOUT_SEC,
    RpcDebugService,
    RpcInvokeResult,
)

router = APIRouter(prefix="/api/v1/message/admin/rpc-debug", tags=["rpc-debug"])


class RpcInvokeReq(BaseModel):
    """发起一次 RPC 调试调用。

    `payload_json` 是**原始 JSON 文本**（前端文本域直接贴）：不同方法的参数形态不同，
    无法静态建模；服务端会按该方法的 params 契约严格校验后投递。
    """

    method_name: str = Field(description="方法名（见 GET /methods 返回列表）")
    payload_json: str = Field(default="{}", description="请求参数（JSON 文本）")
    timeout: float = Field(
        default=DEFAULT_TIMEOUT_SEC, description="超时秒数（1~30，默认 5）"
    )


@router.get("/methods", summary="列出可测试的 RPC 方法（契约登记表）")
async def list_methods(user: RootUser) -> StandardResponse[list]:
    """列出全部契约登记过的 RPC 方法（方法名 / 归属服务 / routing_key / 参数 Schema）。"""
    return StandardResponse(data=RpcDebugService.methods())


@router.post("/invoke", summary="发起一次真实 RPC 往返（回显原始信封）")
async def invoke_rpc(user: RootUser, req: RpcInvokeReq) -> StandardResponse[RpcInvokeResult]:
    """按契约方法发起真实 RPC，回显 `StandardResponse` 原始信封（失败也回显，不吞错）。

    - 未知方法 / 参数不合法：400 回包（错误详情在 msg 里），不投递消息；
    - RPC 超时 / 未连接：500 回包（msg 带原因）。
    """
    try:
        result = await RpcDebugService.invoke(
            method_name=req.method_name,
            payload_json=req.payload_json,
            timeout=req.timeout,
        )
    except KeyError as e:
        return StandardResponse(
            code=404, msg=f"未知 RPC 方法: {e.args[0] if e.args else e}（见 GET /methods）"
        )
    except (ValueError, TypeError) as e:
        # ValueError：JSON 解析失败 / 不符合契约；TypeError：payload 不是 JSON 对象
        return StandardResponse(code=400, msg=str(e))
    except TimeoutError as e:
        return StandardResponse(code=504, msg=f"RPC 调用超时: {e}")
    except ConnectionError as e:
        return StandardResponse(code=503, msg=f"RPC 连接不可用: {e}")
    except Exception as e:  # noqa: BLE001 - 调试接口必须把任何异常原样回显给调用方
        return StandardResponse(code=500, msg=f"{type(e).__name__}: {e}")

    return StandardResponse(data=result)


__all__ = ["router"]
