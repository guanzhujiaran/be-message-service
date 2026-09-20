"""前端跳转目标构造与校验（计划书 §2.10）。

后端下发给前端的站内跳转**只给前端路由名**：``route:{路由名}?{query}``。
路由名由 :class:`app.models.enums.FrontendRouteEnum` 收敛（值 = 前端路由的 ``name``），
路径只在前端路由表写一次，后端不再硬编码任何 ``/app/...`` 路径。

构造统一走 :func:`build_route_target`，校验统一走 :func:`parse_route_name`
（未注册的路由名一律视为非法，由通知正文降级成纯文本，不渲染成链接）。
"""

from __future__ import annotations

from collections.abc import Mapping
from urllib.parse import urlencode

from bili_common.models import InteractionBizTypeEnum

from app.models.enums import FrontendRouteEnum

__all__ = [
    "ROUTE_SCHEME",
    "build_route_target",
    "is_route_target",
    "jump_target_for",
    "parse_route_name",
]

#: 站内跳转目标的前缀（与前端 src/utils/notifyContent.ts 的 ROUTE_SCHEME 必须一致）
ROUTE_SCHEME = "route:"


def build_route_target(
    route: FrontendRouteEnum,
    params: Mapping[str, object] | None = None,
) -> str:
    """拼出 ``route:{路由名}?{query}`` 形式的跳转目标。

    空值（``None`` / 空串）参数会被跳过，避免拼出 ``rpid=`` 这种空查询。
    """
    target = f"{ROUTE_SCHEME}{route.value}"
    if not params:
        return target
    query = urlencode(
        {k: v for k, v in params.items() if v is not None and v != ""},
        safe=":",
    )
    return f"{target}?{query}" if query else target


def parse_route_name(url: str | None) -> str | None:
    """从 ``route:{name}?{query}`` 解析出路由名。

    目标不是 ``route:`` 形态、或路由名未在 :class:`FrontendRouteEnum` 注册时返回 ``None``
    （调用方据此降级，不再渲染成可点击链接）。
    """
    if not url or not url.startswith(ROUTE_SCHEME):
        return None
    name = url[len(ROUTE_SCHEME) :].split("?", 1)[0].strip()
    if not name:
        return None
    try:
        FrontendRouteEnum.from_name(name)
    except ValueError:
        return None
    return name


def is_route_target(url: str | None) -> bool:
    """是否为合法的「路由名跳转」目标。"""
    return parse_route_name(url) is not None


#: 资源类型 →（前端路由名, 资源 id 在路由参数里的 key）
_JUMP_ROUTE_MAP: dict[InteractionBizTypeEnum, tuple[FrontendRouteEnum, str]] = {
    InteractionBizTypeEnum.DYNAMIC: (FrontendRouteEnum.MOMENT_DETAIL, "momentId"),
    InteractionBizTypeEnum.LOTTERY: (FrontendRouteEnum.LOTTERY_CARD_DETAIL, "id"),
    # 2.61.0：第三方抽奖动态没有 lottery_id，不能复用抽奖卡片详情页（那按 lottery_id 拉详情）
    InteractionBizTypeEnum.OTHERS_LOT_DYN: (
        FrontendRouteEnum.OTHERS_LOT_DYN_DETAIL,
        "dynId",
    ),
}


def jump_target_for(
    biz_type: InteractionBizTypeEnum | str | int,
    biz_id: int | str,
    rpid: str | int | None = None,
) -> str | None:
    """按资源类型拼出后端下发的跳转目标 ``route:{name}?{query}``。

    未知 / 无资源 id 的资源类型返回 ``None``（前端不跳转）。``rpid`` 为可选楼层锚点
    （评论定位用，写入 ``rpid`` 查询参数）。
    """
    try:
        bt = (
            biz_type
            if isinstance(biz_type, InteractionBizTypeEnum)
            else InteractionBizTypeEnum(biz_type)
        )
    except (ValueError, KeyError):
        return None
    entry = _JUMP_ROUTE_MAP.get(bt)
    if not entry or not biz_id:
        return None
    route, key = entry
    params: dict[str, object] = {key: str(biz_id)}
    if rpid:
        params["rpid"] = str(rpid)
    return build_route_target(route, params)
