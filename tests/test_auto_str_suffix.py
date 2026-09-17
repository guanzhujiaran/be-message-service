"""``@auto_str`` / ``AutoStrMixin`` 字符串版 ID 字段回归测试。

覆盖点：
1. 默认后缀仍为 ``Str``；``suffix="_str"`` 可切 snake_case；
2. 非 ID 数值字段（like_count）不派生；
3. 已声明的同名字段（如手写 ``dynIdStr: str``）不被 computed_field 覆盖；
4. 类体里手写的 ``@computed_field`` 在后置注入 + 重建后不丢失；
5. 遗留 ``AutoStrMixin`` 行为不变（仍读 ``_auto_str_suffix`` 类属性）。
"""

from typing import ClassVar

from bili_common.models import AdminStatusResponse
from bili_common.models.auto_str import AutoStrMixin, SnowflakeInt, auto_str
from pydantic import computed_field
from sqlmodel import SQLModel

from app.models.str_int import StrInt

SNOWFLAKE = 1342368973191234567  # 19 位，超过 JS Number 安全整数


@auto_str
class _DefaultSuffixModel(SQLModel):
    """不传 suffix：沿用默认 Str 后缀。"""

    mid: int
    like_count: int


@auto_str(suffix="_str")
class _SnakeSuffixModel(SQLModel):
    mid: int
    like_count: int


class _ClassAttrSuffixModel(SQLModel):
    _auto_str_suffix: ClassVar[str] = "_str"

    mid: int


auto_str(_ClassAttrSuffixModel)


@auto_str
class _DeclaredStrFieldModel(SQLModel):
    """已手写 dynIdStr 字段：不得被 computed_field 覆盖。"""

    dynId: int
    dynIdStr: str = "manual"


@auto_str
class _ManualComputedFieldModel(SQLModel):
    """类体里手写的 computed_field：重建 decorators 后必须还在。"""

    mid: int

    @computed_field  # type: ignore[prop-decorator]
    @property
    def business_name(self) -> str:
        return "business"


@auto_str
class _MarkedModel(SQLModel):
    """SnowflakeInt 标记：无论命名都派生。"""

    some_code: SnowflakeInt


def test_default_suffix_is_camel_str():
    dumped = _DefaultSuffixModel(mid=SNOWFLAKE, like_count=3).model_dump()
    assert dumped["midStr"] == str(SNOWFLAKE)
    assert "mid_str" not in dumped
    # 非 ID 数值字段不派生
    assert "like_countStr" not in dumped


def test_custom_suffix_is_snake_str():
    dumped = _SnakeSuffixModel(mid=SNOWFLAKE, like_count=3).model_dump()
    assert dumped["mid_str"] == str(SNOWFLAKE)
    assert "midStr" not in dumped


def test_class_attr_suffix():
    dumped = _ClassAttrSuffixModel(mid=SNOWFLAKE).model_dump()
    assert dumped["mid_str"] == str(SNOWFLAKE)


def test_custom_suffix_visible_in_openapi_serialization_schema():
    props = _SnakeSuffixModel.model_json_schema(mode="serialization")["properties"]
    assert "mid_str" in props
    assert "midStr" not in props


def test_declared_field_not_overridden():
    """回归：pydantic 建类时会 delattr 字段属性，只看 hasattr 会漏判并抛
    "Field ... overrides symbol of same name in a parent class"。"""
    dumped = _DeclaredStrFieldModel(dynId=SNOWFLAKE).model_dump()
    assert dumped["dynIdStr"] == "manual"


def test_manual_computed_field_preserved():
    """回归：DecoratorInfos 重建只能扫到后置注入的 proxy，类体手写的会丢。"""
    dumped = _ManualComputedFieldModel(mid=SNOWFLAKE).model_dump()
    assert dumped["business_name"] == "business"
    assert dumped["midStr"] == str(SNOWFLAKE)


@auto_str
class _OptionalSnowflakeModel(SQLModel):
    """可选雪花 ID（``StrInt | None``）同样派生，字符串版为 ``str | None``。"""

    bizId: StrInt | None = None


def test_optional_str_int_derives_nullable_str():
    assert _OptionalSnowflakeModel(bizId=None).model_dump()["bizIdStr"] is None
    dumped = _OptionalSnowflakeModel(bizId=SNOWFLAKE).model_dump()
    assert dumped["bizIdStr"] == str(SNOWFLAKE)

    prop = _OptionalSnowflakeModel.model_json_schema(mode="serialization")["properties"]["bizIdStr"]
    assert {"string", "null"} <= {item["type"] for item in prop["anyOf"]}


def test_snowflake_marker():
    dumped = _MarkedModel(some_code=SNOWFLAKE).model_dump()
    assert dumped["some_codeStr"] == str(SNOWFLAKE)


def test_legacy_mixin_keeps_working():
    class Legacy(SQLModel, AutoStrMixin):
        _auto_str_suffix: ClassVar[str] = "_str"

        mid: int

    assert Legacy(mid=SNOWFLAKE).model_dump()["mid_str"] == str(SNOWFLAKE)


def test_admin_status_response_uses_snake_suffix():
    """`GET /api/v1/message/admin/me` 出参整体 snake_case，字符串版 mid 用 _str。"""
    dumped = AdminStatusResponse(
        is_root=True, is_admin=True, biz_perms={"*": 7}, mid=SNOWFLAKE
    ).model_dump()
    assert dumped["mid_str"] == str(SNOWFLAKE)
    assert "midStr" not in dumped
