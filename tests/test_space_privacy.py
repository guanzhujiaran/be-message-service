"""空间对外可见性（2.58.0）纯逻辑单元测试。

只覆盖**不依赖 DB / pptr / mmdb** 的纯函数部分（身份判定与字段裁剪），
避免把网络与库状态带进单测；依赖 DB 的读写路径由接口层与集成测试覆盖。
"""

from datetime import datetime

from app.models.schemas.space import SpaceInfoResp, SpacePrivacyFlags
from app.services.user.space.attributes import SPACE_ATTRIBUTES, AttributeKey
from app.services.user.space.visibility import SpaceVisibility
from app.services.user.space_privacy import SpacePrivacyService

MID = 920041
OTHER = 920042


def _visibility(viewer: int | None, **flags: bool) -> SpaceVisibility:
    """构造「主人 = MID」的可见性上下文（关键字即开关名）。"""
    return SpaceVisibility(
        owner_mid=MID, viewer_mid=viewer, flags=SpacePrivacyFlags(**flags)
    )


def test_can_see_private_only_self() -> None:
    """只有「本人」可见敏感字段；未登录与他人一律 False。"""
    assert SpacePrivacyService.can_see_private(MID, MID) is True
    assert SpacePrivacyService.can_see_private(MID, OTHER) is False
    assert SpacePrivacyService.can_see_private(MID, None) is False


def test_all_off_hides_personal_info_for_visitor() -> None:
    """他人 + 开关全关：个人资料（邮箱 / 性别 / 生日）被裁（配合路由 exclude_none 即不下发）。"""
    info = SpaceInfoResp(mid=MID, email="a@b.c", sex="男", birthday=631152000)
    _visibility(OTHER).mask_profile(info)
    assert info.email is None
    assert info.sex == ""
    assert info.birthday == 0


def test_login_info_is_always_public() -> None:
    """2026-10-08：上次登录 / IP 属地**不设开关**，他人视角也不裁剪（固定公开）。"""
    info = SpaceInfoResp(
        mid=MID, ip_location="浙江 杭州", last_login_at=datetime(2026, 10, 1)
    )
    _visibility(OTHER).mask_profile(info)
    assert info.ip_location == "浙江 杭州"
    assert info.last_login_at == datetime(2026, 10, 1)


def test_owner_sees_everything() -> None:
    """本人：即使开关全关也不裁剪（自己的空间自己看）。"""
    info = SpaceInfoResp(mid=MID, email="a@b.c", ip_location="浙江 杭州")
    _visibility(MID).mask_profile(info)
    assert info.email == "a@b.c"
    assert info.ip_location == "浙江 杭州"


def test_personal_info_switch_keeps_login_info() -> None:
    """开关互相独立：打开个人资料则邮箱保留；登录信息本来就恒公开。"""
    info = SpaceInfoResp(mid=MID, email="a@b.c", ip_location="浙江 杭州")
    _visibility(OTHER, show_personal_info=True).mask_profile(info)
    assert info.email == "a@b.c"
    assert info.ip_location == "浙江 杭州"


def test_login_info_attribute_removed() -> None:
    """`show_login_info` 已废弃：属性注册表不再包含 LOGIN_INFO 维度。"""
    assert not hasattr(AttributeKey, "LOGIN_INFO")
    assert not hasattr(AttributeKey, "FAVORITES")
    assert AttributeKey.PERSONAL_INFO in {attr.key for attr in SPACE_ATTRIBUTES}
    assert AttributeKey.TIMELINE in {attr.key for attr in SPACE_ATTRIBUTES}
    assert len(SPACE_ATTRIBUTES) == 5


def test_timeline_defaults_public() -> None:
    """时间线总开关**默认开启**（与其它隐私开关「默认关闭」相反）。"""
    attr = next(a for a in SPACE_ATTRIBUTES if a.key is AttributeKey.TIMELINE)
    assert attr.default_value() is True


def test_resolve_location_handles_bad_input() -> None:
    """属地解析：空 IP / 非法 IP 都不抛异常（降级为空，避免空间页报错）。"""
    assert SpacePrivacyService._resolve_location(None) is None
    assert SpacePrivacyService._resolve_location("") is None
    assert SpacePrivacyService._resolve_location("not-an-ip") is None
