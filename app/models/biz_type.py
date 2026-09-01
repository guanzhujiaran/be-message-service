"""业务类型（biz_type）唯一真相源：关联映射 + 展示名。

见计划书 §5.9：``InteractionBizTypeEnum``（bili-common）是**业务资源类型的唯一真相源**，
事件来源（source_type）也直接复用本枚举成员，
语义一律经本模块与 biz_type 建立关联。

**展示名只在这里定义一份**：事件 msgfeed 的 ``business_name``、审核来源的 ``label``、
系统通知正文里的来源名，全部调用本模块的函数，禁止各模块再自建「类型 → 中文名」字典
（历史上有三份重复定义：``schemas/event.py`` / ``utils/audit_source.py`` /
``services/message/insite/events.py``，已全部收敛到此处）。
"""

from __future__ import annotations

from bili_common.models.interaction import InteractionBizTypeEnum

__all__ = [
    "biz_type_label",
    "biz_type_to_comment_type",
    "biz_type_to_source_type",
    "comment_type_label",
    "comment_type_to_biz_type",
    "source_type_label",
    "source_type_to_biz_type",
]

#: 未知 / 缺省业务类型的展示名
_DEFAULT_LABEL = "其他"

#: biz_type → 展示名（唯一来源；key 用 ``InteractionBizTypeEnum`` 成员）
_BIZ_TYPE_LABEL: dict[InteractionBizTypeEnum, str] = {
    InteractionBizTypeEnum.DYNAMIC: "动态",
    InteractionBizTypeEnum.LOTTERY: "抽奖",
    InteractionBizTypeEnum.RPA_ACTION: "RPA动作",
    InteractionBizTypeEnum.RPA_WORKFLOW: "RPA工作流",
    InteractionBizTypeEnum.RPA_BROWSER: "RPA浏览器",
    InteractionBizTypeEnum.RPA_PLUGIN: "RPA插件",
    InteractionBizTypeEnum.COMMENT: "评论",
    InteractionBizTypeEnum.USER: "用户",
}

#: 事件来源实体类型 ↔ biz_type（无对应关系的成员不入表，查询返回 ``None``）。
#: 评论 / 用户空间等资源类型自身即为业务资源，直接映射到自身；
#: 视频 / 专栏 / 其他等无对应资源的来源**不进入枚举**，避免写入无对应资源的事件。
_SOURCE_TYPE_TO_BIZ_TYPE: dict[InteractionBizTypeEnum, InteractionBizTypeEnum] = {
    InteractionBizTypeEnum.DYNAMIC: InteractionBizTypeEnum.DYNAMIC,
    InteractionBizTypeEnum.LOTTERY: InteractionBizTypeEnum.LOTTERY,
    InteractionBizTypeEnum.COMMENT: InteractionBizTypeEnum.COMMENT,
    InteractionBizTypeEnum.USER: InteractionBizTypeEnum.USER,
}
_COMMENT_TYPE_TO_BIZ_TYPE: dict[InteractionBizTypeEnum, InteractionBizTypeEnum] = {
    InteractionBizTypeEnum.DYNAMIC: InteractionBizTypeEnum.DYNAMIC,
    InteractionBizTypeEnum.LOTTERY: InteractionBizTypeEnum.LOTTERY,
}


def biz_type_label(biz_type: InteractionBizTypeEnum | str | int | None) -> str:
    """biz_type 的中文展示名（未知回落「其他」）。

    入参支持枚举成员 / 对外文字（``lottery``）/ 数值，统一经
    ``InteractionBizTypeEnum.from_text`` 归一，与跨服务契约保持一致。
    """
    if biz_type is None:
        return _DEFAULT_LABEL
    try:
        bt = InteractionBizTypeEnum.from_text(biz_type)
    except (ValueError, KeyError):
        return _DEFAULT_LABEL
    return _BIZ_TYPE_LABEL.get(bt, bt.to_text())


def source_type_to_biz_type(
    source_type: InteractionBizTypeEnum | int | None,
) -> InteractionBizTypeEnum | None:
    """事件来源实体类型 → biz_type；无对应关系（视频 / 专栏 / 其他无对应资源的来源）返回 ``None``。"""
    if source_type is None:
        return None
    try:
        st = InteractionBizTypeEnum(source_type)
    except ValueError:
        return None
    return _SOURCE_TYPE_TO_BIZ_TYPE.get(st)


def biz_type_to_source_type(
    biz_type: InteractionBizTypeEnum | str | int | None,
) -> InteractionBizTypeEnum:
    """biz_type → 事件来源实体类型。

    仅 ``DYNAMIC`` / ``LOTTERY`` 有对应来源类型；其余 biz_type 无合法事件来源，
    直接抛 ``ValueError``（不再回落占位 ``OTHER``，避免写入无对应资源的事件）。
    """
    if biz_type is None:
        raise ValueError("biz_type 为空，无法确定事件来源实体类型")
    try:
        bt = InteractionBizTypeEnum.from_text(biz_type)
    except (ValueError, KeyError) as e:
        raise ValueError(f"无法识别的 biz_type: {biz_type!r}") from e
    for st, candidate in _SOURCE_TYPE_TO_BIZ_TYPE.items():
        if candidate is bt:
            return st
    raise ValueError(f"biz_type {biz_type!r} 无对应的事件来源实体类型")


def comment_type_to_biz_type(
    comment_type: InteractionBizTypeEnum | int | None,
) -> InteractionBizTypeEnum | None:
    """评论区类型 → biz_type；无对应关系返回 ``None``。"""
    if comment_type is None:
        return None
    try:
        ct = InteractionBizTypeEnum(comment_type)
    except ValueError:
        return None
    return _COMMENT_TYPE_TO_BIZ_TYPE.get(ct)


def biz_type_to_comment_type(
    biz_type: InteractionBizTypeEnum | str | int | None,
) -> InteractionBizTypeEnum:
    """biz_type → 评论区类型；无对应评论区的 biz_type 抛 ``ValueError``（不回落占位值）。"""
    if biz_type is None:
        raise ValueError("biz_type 为空，无法确定评论区类型")
    try:
        bt = InteractionBizTypeEnum.from_text(biz_type)
    except (ValueError, KeyError) as e:
        raise ValueError(f"无法识别的 biz_type: {biz_type!r}") from e
    for ct, candidate in _COMMENT_TYPE_TO_BIZ_TYPE.items():
        if candidate is bt:
            return ct
    raise ValueError(f"biz_type {biz_type!r} 无对应的评论区类型")


def source_type_label(source_type: InteractionBizTypeEnum | int | None) -> str:
    """事件来源实体类型的展示名：取对应 biz_type 的展示名，无对应业务资源类型回落「其他」。"""
    if source_type is None:
        return _DEFAULT_LABEL
    biz_type = source_type_to_biz_type(source_type)
    if biz_type is not None:
        return biz_type_label(biz_type)
    return _DEFAULT_LABEL


def comment_type_label(comment_type: InteractionBizTypeEnum | int | None) -> str:
    """评论区类型的展示名：取对应 biz_type 的展示名，未知回落「其他」。"""
    if comment_type is None:
        return _DEFAULT_LABEL
    biz_type = comment_type_to_biz_type(comment_type)
    if biz_type is not None:
        return biz_type_label(biz_type)
    return _DEFAULT_LABEL
