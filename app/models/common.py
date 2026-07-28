"""跨模块通用的响应模型。"""

from pydantic import BaseModel


# 与 RPA-Browser 的 StandardResponse 保持同构（code / data / msg）
class StandardResponse(BaseModel):
    """所有 HTTP 接口的统一返回结构。"""

    code: int = 0
    data: object | None = None
    msg: str = "success"
