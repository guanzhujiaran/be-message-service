"""
pptr 用户相关 RPC 服务端（be-message 为服务端）。

be-gateway（Node.js）通过 amqplib 调本模块的 RPC 方法，完成用户读 / 创建 / 更新，
彻底不再维护本地 sequelize 用户表。契约（方法名 / 请求 / 响应）统一来自 bili_common。

路由键前缀：`message.pptr.rpc.<method_name>`（见 bili_common PPTR_RPC_ROUTING_KEY_PREFIX）。
本模块只需被 main.py import 一次即可完成 RPC 注册（FastStream 全局 broker 单例）。
"""

from bili_common.models import (
    PptrAddDailyLoginExpParams,
    PptrAddDailyLoginExpResult,
    PptrAddExpParams,
    PptrAddExpResult,
    PptrAddUsernameRecordParams,
    PptrAddUsernameRecordResult,
    PptrCreateUserParams,
    PptrCreateUserResult,
    PptrGetUserCardParams,
    PptrGetUserInfoParams,
    PptrGetUserLevelParams,
    PptrGetUserNavParams,
    PptrSetResult,
    PptrSetUserDetailParams,
    PptrSetUserLevelParams,
    PptrSetUserRoleParams,
    PptrUpdateUserInfoParams,
    PptrUpdateUserInfoResult,
    PptrUserCard,
    PptrUserLevelInfo,
    PptrUserProfile,
    PptrUserSearchResult,
    RpcMethodName,
    StandardResponse,
    UserSearchParams,
    error_response,
    pptr_routing_key_for,
    success_response,
)
from faststream.rabbit import RabbitQueue

from bili_common.rpc.safe import rpc_safe
from loguru import logger

from app.core.broker import broker, message_exchange
from app.services.message.notify import NotifyService
from app.services.user.pptr_user import PptrUserService, _level_calc


async def _push_new_user_notify(
    *, user_name: str, uid: int, uname: str = ""
) -> bool:
    """新建用户后发布「欢迎注册」系统通知（写入 msg_notify，站内信）。

    委托 `NotifyService.send_welcome` 统一实现（Casdoor 登录注册通道共用同一逻辑），
    避免 OAuth 通道漏发欢迎消息。弱依赖，失败仅记 ERROR 告警，不阻断创建用户 RPC。

    Returns:
        bool: 是否发布成功。
    """
    nickname = uname or user_name
    try:
        await NotifyService.send_welcome(uid, nickname)
    except Exception as e:  # noqa: BLE001
        logger.error(f"新建用户 {uid} 的欢迎系统通知发布失败（弱依赖，已忽略）: {e}")
        return False
    return True


def _profile_to_dto(info, detail, vip, level) -> PptrUserProfile:
    """把四张表原始记录拼成对外档案 DTO（face 映射 TUserDetail.avatar）。"""
    return PptrUserProfile(
        uid=int(info.uid),
        user_name=info.user_name or "",
        role=info.role or "",
        pwd=info.pwd or "",
        createdAt=info.createdAt.isoformat() if info.createdAt else "",
        level=(
            int(level.current_level) if level and level.current_level is not None else 0
        ),
        uname=detail.uname if detail else "",
        face=detail.avatar if detail else None,
        email=detail.email if (detail and detail.email is not None) else "",
        current_level=(
            int(level.current_level) if level and level.current_level is not None else 0
        ),
        vip_type=int(vip.vip_type) if vip and vip.vip_type is not None else 0,
        vip_due_date=(
            int(vip.vip_due_date) if vip and vip.vip_due_date is not None else 0
        ),
        vip_status=int(vip.vip_status) if vip and vip.vip_status is not None else 0,
    )


@broker.subscriber(
    queue=RabbitQueue(
        pptr_routing_key_for(RpcMethodName.GET_USER_INFO),
        routing_key=pptr_routing_key_for(RpcMethodName.GET_USER_INFO),
        durable=True,
    ),
    exchange=message_exchange,
)
@rpc_safe
async def rpc_get_user_info(params: PptrGetUserInfoParams) -> StandardResponse:
    """按 uid 或 user_name 返回完整用户信息（get_user_info）。"""
    row = await PptrUserService.get_user_profile(
        uid=params.uid or None, user_name=params.user_name or None
    )
    if not row:
        return error_response(
            code=404,
            msg=f"用户不存在 uid={params.uid} user_name={params.user_name}",
            data={"uid": params.uid, "user_name": params.user_name},
        )
    info, detail, vip, level = row
    return success_response(data=_profile_to_dto(info, detail, vip, level))


@broker.subscriber(
    queue=RabbitQueue(
        pptr_routing_key_for(RpcMethodName.GET_USER_CARD),
        routing_key=pptr_routing_key_for(RpcMethodName.GET_USER_CARD),
        durable=True,
    ),
    exchange=message_exchange,
)
@rpc_safe
async def rpc_get_user_card(params: PptrGetUserCardParams) -> StandardResponse:
    """按 uid 返回卡片简略信息（get_user_card）。"""
    row = await PptrUserService.get_user_profile(uid=params.uid)
    if not row:
        return error_response(
            code=404,
            msg=f"用户不存在 uid={params.uid}",
            data={"uid": params.uid},
        )
    info, detail, vip, level = row
    return success_response(
        data=PptrUserCard(
            uid=int(info.uid),
            user_name=info.user_name or "",
            uname=detail.uname if detail else "",
            face=detail.avatar if detail else None,
        )
    )


@broker.subscriber(
    queue=RabbitQueue(
        pptr_routing_key_for(RpcMethodName.CREATE_USER),
        routing_key=pptr_routing_key_for(RpcMethodName.CREATE_USER),
        durable=True,
    ),
    exchange=message_exchange,
)
@rpc_safe
async def rpc_create_user(params: PptrCreateUserParams) -> StandardResponse:
    """创建用户（create_user）：一次性写入 TUserInfo + TUserDetail + TUserLevel + TUserVip。

    uid 为 0 时由 Postgres 自增；返回真实 uid 与是否新建。
    """
    uid, created = await PptrUserService.create_user(
        uid=params.uid or 0,
        user_name=params.user_name,
        pwd=params.pwd,
        createdAt=params.createdAt or None,
        uname=params.uname,
        face=params.face,
        sign=params.sign,
        sex=params.sex,
        email=params.email,
        birthday=params.birthday,
        current_level=params.current_level,
        vip_type=params.vip_type,
        vip_due_date=params.vip_due_date,
        vip_status=params.vip_status,
        ip=params.ip,
        ua=params.ua,
    )
    # 新建用户后发布一条「欢迎注册」系统通知（写入 msg_notify，弱依赖、不阻断 RPC 返回）。
    # 仅 created=True 时发布：create_user 对已存在用户是 upsert（Casdoor 登录每次都会调用），
    # 不该给老用户反复发欢迎通知。
    if created:
        await _push_new_user_notify(
            user_name=params.user_name, uid=uid, uname=params.uname or ""
        )
    return success_response(data=PptrCreateUserResult(uid=uid, created=created))


@broker.subscriber(
    queue=RabbitQueue(
        pptr_routing_key_for(RpcMethodName.UPDATE_USER_INFO),
        routing_key=pptr_routing_key_for(RpcMethodName.UPDATE_USER_INFO),
        durable=True,
    ),
    exchange=message_exchange,
)
@rpc_safe
async def rpc_update_user_info(params: PptrUpdateUserInfoParams) -> StandardResponse:
    """更新用户（update_user_info）：按 uid/user_name 更新 pwd / reg_ip_info_id。"""
    uid, updated = await PptrUserService.update_user_info(
        uid=params.uid or 0,
        user_name=params.user_name or "",
        pwd=params.pwd if params.pwd != "" else None,
        reg_ip_info_id=params.reg_ip_info_id,
    )
    return success_response(data=PptrUpdateUserInfoResult(uid=uid, updated=updated))


@broker.subscriber(
    queue=RabbitQueue(
        pptr_routing_key_for(RpcMethodName.GET_USER_LEVEL),
        routing_key=pptr_routing_key_for(RpcMethodName.GET_USER_LEVEL),
        durable=True,
    ),
    exchange=message_exchange,
)
@rpc_safe
async def rpc_get_user_level(params: PptrGetUserLevelParams) -> StandardResponse:
    """按 uid 取等级信息（get_user_level），next_exp 由 be-message 按经验配置计算。"""
    lv = await PptrUserService.get_user_level(params.uid)
    if lv is None:
        return error_response(
            code=404, msg=f"用户不存在 uid={params.uid}", data={"uid": params.uid}
        )
    current_level, current_exp, current_min, updated_at = lv
    calc = _level_calc(current_exp, uid=params.uid)
    return success_response(
        data=PptrUserLevelInfo(
            uid=params.uid,
            current_level=current_level,
            current_exp=current_exp,
            current_min=current_min,
            next_exp=calc.next_exp,
            updated_at=updated_at,
        )
    )


@broker.subscriber(
    queue=RabbitQueue(
        pptr_routing_key_for(RpcMethodName.SET_USER_LEVEL),
        routing_key=pptr_routing_key_for(RpcMethodName.SET_USER_LEVEL),
        durable=True,
    ),
    exchange=message_exchange,
)
@rpc_safe
async def rpc_set_user_level(params: PptrSetUserLevelParams) -> StandardResponse:
    """原子写入等级经验（set_user_level）。"""
    ok = await PptrUserService.set_user_level(
        uid=params.uid,
        current_level=params.current_level,
        current_exp=params.current_exp,
        current_min=params.current_min,
    )
    return success_response(data=PptrSetResult(uid=params.uid, updated=ok))


@broker.subscriber(
    queue=RabbitQueue(
        pptr_routing_key_for(RpcMethodName.SET_USER_DETAIL),
        routing_key=pptr_routing_key_for(RpcMethodName.SET_USER_DETAIL),
        durable=True,
    ),
    exchange=message_exchange,
)
@rpc_safe
async def rpc_set_user_detail(params: PptrSetUserDetailParams) -> StandardResponse:
    """更新用户详情（set_user_detail）。"""
    ok = await PptrUserService.set_user_detail(
        uid=params.uid,
        uname=params.uname,
        face=params.face,
        sign=params.sign,
        sex=params.sex,
        email=params.email,
        birthday=params.birthday,
    )
    return success_response(data=PptrSetResult(uid=params.uid, updated=ok))


@broker.subscriber(
    queue=RabbitQueue(
        pptr_routing_key_for(RpcMethodName.SET_USER_ROLE),
        routing_key=pptr_routing_key_for(RpcMethodName.SET_USER_ROLE),
        durable=True,
    ),
    exchange=message_exchange,
)
@rpc_safe
async def rpc_set_user_role(params: PptrSetUserRoleParams) -> StandardResponse:
    """更新用户角色（set_user_role，root 受保护）。"""
    ok = await PptrUserService.set_user_role(uid=params.uid, role=params.role)
    return success_response(data=PptrSetResult(uid=params.uid, updated=ok))


@broker.subscriber(
    queue=RabbitQueue(
        pptr_routing_key_for(RpcMethodName.SEARCH_USERS),
        routing_key=pptr_routing_key_for(RpcMethodName.SEARCH_USERS),
        durable=True,
    ),
    exchange=message_exchange,
)
@rpc_safe
async def rpc_search_users(params: UserSearchParams) -> StandardResponse:
    """管理端用户搜索（search_users，复用 PptrUserService.search_users）。"""
    items, has_more = await PptrUserService.search_users(
        keyword=params.keyword or "",
        offset=params.offset or 0,
        limit=params.limit or 20,
    )
    return success_response(data=PptrUserSearchResult(items=items, has_more=has_more))


@broker.subscriber(
    queue=RabbitQueue(
        pptr_routing_key_for(RpcMethodName.ADD_EXP),
        routing_key=pptr_routing_key_for(RpcMethodName.ADD_EXP),
        durable=True,
    ),
    exchange=message_exchange,
)
@rpc_safe
async def rpc_add_exp(params: PptrAddExpParams) -> StandardResponse:
    """增加经验值（add_exp）：业务逻辑在 be-message 侧完成（经验计算 + 升级角色同步）。"""
    result = await PptrUserService.add_exp(uid=params.uid, exp=params.exp, action_type=params.action_type)
    return success_response(data=PptrAddExpResult(**result))


@broker.subscriber(
    queue=RabbitQueue(
        pptr_routing_key_for(RpcMethodName.ADD_DAILY_LOGIN_EXP),
        routing_key=pptr_routing_key_for(RpcMethodName.ADD_DAILY_LOGIN_EXP),
        durable=True,
    ),
    exchange=message_exchange,
)
@rpc_safe
async def rpc_add_daily_login_exp(
    params: PptrAddDailyLoginExpParams,
) -> StandardResponse:
    """每日首次登录加经验（add_daily_login_exp）：业务逻辑在 be-message 侧完成。

    每日幂等（基于 TUserLevel.updatedAt 跨 0 点）+ 经验计算 + 升级角色同步。
    """
    result = await PptrUserService.add_daily_login_exp(uid=params.uid)
    return success_response(data=PptrAddDailyLoginExpResult(**result))


@broker.subscriber(
    queue=RabbitQueue(
        pptr_routing_key_for(RpcMethodName.ADD_USERNAME_RECORD),
        routing_key=pptr_routing_key_for(RpcMethodName.ADD_USERNAME_RECORD),
        durable=True,
    ),
    exchange=message_exchange,
)
@rpc_safe
async def rpc_add_username_record(
    params: PptrAddUsernameRecordParams,
) -> StandardResponse:
    """记录昵称历史（add_username_record）：由 be-message 直连 pptr Postgres 写入 TUserNameRecord。"""
    created = await PptrUserService.add_username_record(
        uid=params.uid, prev_uname=params.prev_uname
    )
    return success_response(
        data=PptrAddUsernameRecordResult(uid=params.uid, created=created)
    )


@broker.subscriber(
    queue=RabbitQueue(
        pptr_routing_key_for(RpcMethodName.GET_USER_NAV),
        routing_key=pptr_routing_key_for(RpcMethodName.GET_USER_NAV),
        durable=True,
    ),
    exchange=message_exchange,
)
@rpc_safe
async def rpc_get_user_nav(params: PptrGetUserNavParams) -> StandardResponse:
    """获取用户导航信息（get_user_nav）：一次查询返回 nav 全部数据。

    包含等级计算、邮件脱敏，pptr 一次 RPC 调用即拿齐，不再走 HTTP 反向代理。
    """
    data = await PptrUserService.get_user_nav_data(uid=params.uid)
    if data is None:
        return error_response(code=404, msg="用户不存在")
    return success_response(data=data)


__all__ = [
    "rpc_add_daily_login_exp",
    "rpc_add_exp",
    "rpc_add_username_record",
    "rpc_create_user",
    "rpc_get_user_card",
    "rpc_get_user_info",
    "rpc_get_user_level",
    "rpc_get_user_nav",
    "rpc_search_users",
    "rpc_set_user_detail",
    "rpc_set_user_level",
    "rpc_set_user_role",
    "rpc_update_user_info",
]
