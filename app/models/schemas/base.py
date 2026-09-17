"""响应模型雪花 ID 自动生成 ``*Str`` 字段的统一入口（转发自 bili-common）。

实现已收敛到通用库 ``bili-common``，本文件仅做再导出，避免各 schema 散落 import。

用法：响应模型加装饰器 ``@auto_str`` 即可::

    @auto_str
    class XOut(SQLModel):
        mid: int

详见 ``bili_common.models.auto_str``（``AutoStrMixin`` 为遗留写法，新代码用装饰器）。
"""

from bili_common.models.auto_str import SnowflakeInt, auto_str

__all__ = ["SnowflakeInt", "auto_str"]
