"""用户相关模型（转发至 bili_common 公共包，统一 sqlmodel 来源）。

真实单一来源见 `bili_common.models.depends.AuthInfo`，
字段与 RPA-Browser / nodejs-pptr ProxyEndPort.setUserHeaders 注入的 x-bili-* 头一一对应。
"""

from bili_common.models.depends import AuthInfo as MessageUser

__all__ = ["MessageUser"]
