"""空间对外可见性（2.58.0）纯逻辑单元测试。

只覆盖**不依赖 DB / pptr / mmdb** 的纯函数部分（身份判定与字段裁剪），
避免把网络与库状态带进单测；依赖 DB 的读写路径由接口层与集成测试覆盖。
"""

from app.models.schemas.space import SpaceInfoResp, SpacePrivacyFlags
from app.services.user.space_privacy import SpacePrivacyService

MID = 920041
OTHER = 920042


def test_can_see_private_only_self() -> None:
    """只有「本人」可见敏感字段；未登录与他人一律 False。"""
    assert SpacePrivacyService.can_see_private(MID, MID) is True
    assert SpacePrivacyService.can_see_private(MID, OTHER) is False
    assert SpacePrivacyService.can_see_private(MID, None) is False


def test_apply_flags_all_off_hides_sensitive_for_visitor() -> None:
    """他人 + 开关全关：邮箱 / 上次登录 / 属地置None（配合路由 exclude_none 即不下发）。"""
    info = SpaceInfoResp(mid=MID, email="a@b.c", ip_location="浙江 杭州")
    SpacePrivacyService.apply_flags_to_info(info, SpacePrivacyFlags(), MID, OTHER)
    assert info.email is None
    assert info.ip_location is None
    assert info.last_login_at is None


def test_apply_flags_keeps_everything_for_self() -> None:
    """本人：即使开关全关也不裁剪（自己的空间自己看）。"""
    info = SpaceInfoResp(mid=MID, email="a@b.c", ip_location="浙江 杭州")
    SpacePrivacyService.apply_flags_to_info(info, SpacePrivacyFlags(), MID, MID)
    assert info.email == "a@b.c"
    assert info.ip_location == "浙江 杭州"


def test_apply_flags_respects_each_switch() -> None:
    """两个开关互相独立：只开个人资料时，邮箱保留但登录信息仍被裁掉。"""
    info = SpaceInfoResp(mid=MID, email="a@b.c", ip_location="浙江 杭州")
    flags = SpacePrivacyFlags(show_personal_info=True, show_login_info=False)
    SpacePrivacyService.apply_flags_to_info(info, flags, MID, OTHER)
    assert info.email == "a@b.c"
    assert info.ip_location is None


def test_resolve_location_handles_bad_input() -> None:
    """属地解析：空 IP / 非法 IP 都不抛异常（降级为空，避免空间页报错）。"""
    assert SpacePrivacyService._resolve_location(None) is None
    assert SpacePrivacyService._resolve_location("") is None
    assert SpacePrivacyService._resolve_location("not-an-ip") is None
