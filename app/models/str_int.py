"""雪花 ID 统一类型：StrInt。

业务含义：对外雪花 ID 在 JSON 中既可能以数字（历史存量 / 部分客户端）也可能以
字符串（前端为避免 JS `Number` 精度丢失而统一用 str）传递。后端一律按「可接受
str 或 int，内部归一为 int」处理，避免每个 router / service 手写 `int(...)` 归一。

- 输入 `str`（如 `"134236897319"`）→ 自动 `int(...)` 归一
- 输入 `int` → 原样
- 输出（序列化）始终为 `int`（Python `int` 任意精度无损）

OpenAPI schema 表现为 `anyOf[integer, string]`，hey-api 生成的前端 SDK 参数类型
为 `number | string`，前端封装层可声明 `string` 直接传，不再 422。
"""

from typing import Annotated, Union

from pydantic import BeforeValidator


def _coerce_snowflake(v):
    # bool 是 int 子类，但绝不可能是合法雪花 ID，显式拒绝避免误判
    if isinstance(v, bool):
        raise ValueError("bool is not a valid snowflake id")
    if isinstance(v, int):
        return v
    if isinstance(v, str):
        try:
            return int(v)
        except (ValueError, TypeError):
            raise ValueError(f"invalid snowflake id string: {v!r}")
    if isinstance(v, float):
        return int(v)
    raise ValueError(f"cannot coerce {type(v).__name__} to snowflake id")


StrInt = Annotated[Union[int, str], BeforeValidator(_coerce_snowflake)]
