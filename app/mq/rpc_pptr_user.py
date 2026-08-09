"""
pptr 用户相关 RPC 服务端（be-message 为服务端）。

be-gateway（Node.js）通过 amqplib 调本模块的 RPC 方法，完成用户读 / 创建 / 更新，
彻底不再维护本地 sequelize 用户表。契约（方法名 / 请求 / 响应）统一来自 bili_common。

路由键前缀：`message.pptr.rpc.<method_name>`（见 bili_common PPTR_RPC_ROUTING_KEY_PREFIX）。
本模块只需被 main.py import 一次即可完成 RPC 注册（FastStream 全局 broker 单例）。
"""

import traceback
from functools import wraps

from faststream.rabbit import RabbitQueue
from loguru import logger

from app.core.broker import broker, message_exchange
from app.services.pptr_user import PptrUserService, _level_calc
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
    PptrSetUserLevelParams,
    PptrSetUserDetailParams,
    PptrSetUserRoleParams,
    PptrUpdateUserInfoParams,
    PptrUpdateUserInfoResult,
    PptrUserCard,
    PptrUserLevelInfo,
    PptrUserProfile,
    PptrSetResult,
    PptrUserSearchResult,
    RpcMethodName,
    StandardResponse,
    UserSearchParams,
    error_response,
    pptr_routing_key_for,
    success_response,
)


def rpc_safe(func):
    """RPC 边界：捕获 handler 异常并转为 error_response 回包。

    FastStream 0.7.1 在 RPC handler 抛异常时不会向 reply_to 发送任何响应，
    导致 Node（amqplib）客户端永久等待直至超时（RpcTimeoutError）。为维持
    请求/响应契约，服务端必须在边界捕获异常并返回结构化错误信封（而非吞错），
    客户端据此立即得到错误结果而非超时。

    注意：业务 / service 层仍保持「直接抛错、不静默」的约定；此处只是在
    RPC 传输边界把异常翻译成回包，不构成错误屏蔽。
    """

    @wraps(func)
    async def _wrapper(*args, **kwargs):
        try:
            return await func(*args, **kwargs)
        except Exception as e:
            # 打印完整 traceback 与详细错误信息，便于定位。
            # 注意：Pydantic 的 ValidationError 默认 str 仅显示首个错误字段（如 "updatedAt"），
            # 过于简略，这里额外展开 errors() 拿到完整的字段 / 类型 / 原因列表。
            # 堆栈用 traceback.format_exc() 显式获取并字符串化，避免依赖 loguru 的
            # exception=True（在 async / 事件循环上下文中可能拿不到活跃异常而丢堆栈）。
            detail = str(e)
            _errors = getattr(e, "errors", None)
            if callable(_errors):
                try:
                    detail = f"{detail} | errors={_errors()}"
                except Exception:
                    pass
            stack = traceback.format_exc()
            logger.error(
                "pptr RPC {} 失败: {}: {}\n{}\n------ traceback ------\n{}",
                func.__name__,
                type(e).__name__,
                e,
                detail,
                stack,
            )
            # 业务异常若自带 code（如 ResourceConflictException 的 409），优先保留其业务码，
            # 其余异常统一归为 500，避免前端拿到 500 却实为「昵称冲突」等可识别错误。
            code = getattr(e, "code", None) or 500
            return error_response(code=code, msg=f"{type(e).__name__}: {e}")

    return _wrapper


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
    "rpc_get_user_info",
    "rpc_get_user_card",
    "rpc_create_user",
    "rpc_update_user_info",
    "rpc_get_user_level",
    "rpc_set_user_level",
    "rpc_set_user_detail",
    "rpc_set_user_role",
    "rpc_search_users",
    "rpc_add_exp",
    "rpc_add_daily_login_exp",
    "rpc_add_username_record",
    "rpc_get_user_nav",
]
