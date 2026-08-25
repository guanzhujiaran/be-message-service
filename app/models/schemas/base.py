"""响应模型雪花 ID 自动生成 ``*Str`` 字段的统一基类（转发自 bili-common）。

实现已收敛到通用库 ``bili-common``，本文件仅做再导出，避免各 schema 散落 import。

用法：响应模型 ``class X(SQLModel, AutoStrMixin)`` 即可，详见
``bili_common.models.auto_str``。
"""

from bili_common.models.auto_str import AutoStrMixin, SnowflakeInt

__all__ = ["AutoStrMixin", "SnowflakeInt"]
