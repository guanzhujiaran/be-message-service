"""审核语义态映射工具（阶段一）。

阶段一语义态即 `ResourceAuditStatusEnum`（统一枚举），`to_native` 恒等。
本模块提供**数值 / 旧枚举别名**的互转辅助，供阶段二起把存量表（动态 / 评论 /
私信 / 头像 / 封面）接入统一枚举时使用——避免在业务代码里出现裸整数字面量。

⚠️ 注意：存量各表枚举数值存在错位（动态/话题旧 `AUDITING=1`，评论/私信旧
`NORMAL=1`）。迁移必须按**语义**转换（见方案文档 §6 数值错位警告），不能只做
数值搬运——因此这里**不提供**"旧 int -> 新 int"的机械映射，改由各资源接入时
按语义态显式映射。
"""
from __future__ import annotations

from enum import IntEnum
from typing import TypeVar

from app.services.moderation.state_machine import ResourceAuditStatusEnum

__all__ = [
    "resource_status_of",
    "parse_status",
]

_EnumT = TypeVar("_EnumT", bound=IntEnum)


def resource_status_of(state: ResourceAuditStatusEnum) -> ResourceAuditStatusEnum:
    """语义态 -> 统一枚举（阶段一恒等；为接入方提供统一转换入口，杜绝散落 `state.value`）。"""
    return ResourceAuditStatusEnum(int(state))


def parse_status(value: int | ResourceAuditStatusEnum | IntEnum) -> ResourceAuditStatusEnum:
    """把 int / 任意同值枚举 -> 统一 `ResourceAuditStatusEnum`。

    仅接受已经采用统一数值的表达（阶段二迁移后各表均为统一枚举）。
    迁移中的存量错位枚举不在此处理（见模块 docstring）。
    """
    return ResourceAuditStatusEnum(int(value))
