"""私信模块 HTTP 接口（/api/v1/message/dm）。

一对一实时聊天的全部读写入口：

| 接口                | 说明                                                       |
| ------------------- | ---------------------------------------------------------- |
| `POST /send`        | 发送私信（写扩散：索引×2 + 会话×2 同步，正文异步落分片）    |
| `GET  /sessions`    | 会话列表，可按 relation 拆出「陌生人消息」分组              |
| `POST /session/delete` | 删除会话（仅自己不可见）                                 |
| `GET  /messages`    | 聊天记录，msgkey 游标翻页，正文来自月度分库分表             |
| `POST /delete`      | 删除消息（仅自己视角）                                      |
| `POST /recall`      | 撤回消息（双方不可见 + 物理抹掉分片正文，受时间窗口限制）    |
| `POST /ack`         | 会话已读，未读清零并抬高已读水位                            |
| `GET  /unread`      | 私信未读总数                                                |

**msgkey 一律以字符串传输**：它是 64 位雪花 ID，直接用 number 传给浏览器
会触发 JS 的 `Number.MAX_SAFE_INTEGER` 精度丢失。
"""

from fastapi import APIRouter, Query
from loguru import logger

from app.core.database import SessionDep
from app.dependencies import RequiredUser
from app.models import StandardResponse
from app.models.enums import BanServiceEnum, DmRelationEnum
from app.models.schemas import (
    DmAckReq,
    DmDeleteReq,
    DmMessageListResp,
    DmOperationResp,
    DmRecallReq,
    DmSendReq,
    DmSendResp,
    DmSessionDeleteReq,
    DmSessionListResp,
)
from app.services.activity import ActivityService
from app.services.ban_service import BanService
from app.services.dm import DmService

router = APIRouter(prefix="/api/v1/message/dm", tags=["message-dm"])


def _to_msgkey(raw: str | None) -> int | None:
    """字符串 msgkey → int，非法值返回 None。"""
    if raw is None or not str(raw).strip():
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


@router.post("/send", response_model=StandardResponse[DmSendResp], summary="发送私信")
async def send_dm(
    session: SessionDep, user: RequiredUser, req: DmSendReq
) -> StandardResponse[DmSendResp]:
    """发送一条私信。

    同步部分只写「双方索引行 + 双方会话行」，正文投递到 MQ 由消费者写入
    msgkey 路由到的月度分库分表——发送接口的 RT 不受正文长度与建表 DDL 影响。
    响应里的 `content_async=False` 表示 MQ 不可用，已降级为同步落库。

    若对方关闭了陌生人私信，返回 `filtered=True`：消息只保留在发送方视角。
    """
    # 封禁校验：被封禁「私信」服务的用户禁止发送私信
    if await BanService.is_banned(session, user.mid, BanServiceEnum.DM.value):
        return StandardResponse(code=403, msg="该账号已被封禁私信功能，无法发送私信")

    try:
        data = await DmService.send(session, user.mid, user.uname or user.user_name, req)
    except ValueError as e:
        return StandardResponse(code=400, msg=str(e))
    except Exception as e:  # noqa: BLE001
        logger.exception(f"发送私信失败: {e}")
        return StandardResponse(code=500, msg=f"发送私信失败: {e}")
    return StandardResponse(data=data)


@router.get(
    "/sessions", response_model=StandardResponse[DmSessionListResp], summary="会话列表"
)
async def list_sessions(
    session: SessionDep,
    user: RequiredUser,
    relation: DmRelationEnum | None = Query(
        default=None, description="normal=主列表；stranger=陌生人分组；不传=全部"
    ),
    page_num: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=50),
) -> StandardResponse[DmSessionListResp]:
    await ActivityService.touch(session, user.mid)
    data = await DmService.list_sessions(
        session, user.mid, relation=relation, page_num=page_num, page_size=page_size
    )
    return StandardResponse(data=data)


@router.post(
    "/session/delete",
    response_model=StandardResponse[DmOperationResp],
    summary="删除会话",
)
async def delete_session(
    session: SessionDep, user: RequiredUser, req: DmSessionDeleteReq
) -> StandardResponse[DmOperationResp]:
    affected = await DmService.delete_session(session, user.mid, req.talker_mid)
    return StandardResponse(
        data=DmOperationResp(
            affected=affected, message="已删除会话" if affected else "会话不存在"
        )
    )


@router.get(
    "/messages",
    response_model=StandardResponse[DmMessageListResp],
    summary="拉取聊天记录",
)
async def list_messages(
    session: SessionDep,
    user: RequiredUser,
    talker_mid: int = Query(description="对话方mid"),
    cursor: str | None = Query(
        default=None, description="上一页返回的 cursor（本页最小 msgkey），首屏不传"
    ),
    page_size: int = Query(default=20, ge=1, le=50),
) -> StandardResponse[DmMessageListResp]:
    """按 msgkey 游标倒序翻页拉取聊天记录。

    正文按 msgkey 批量回捞分片；若异步落库尚未完成，
    会回落到索引行冗余的摘要（`content_ready=False`），保证会话流始终可读。
    """
    data = await DmService.list_messages(
        session,
        user.mid,
        talker_mid,
        cursor=_to_msgkey(cursor),
        page_size=page_size,
    )
    return StandardResponse(data=data)


@router.post(
    "/delete", response_model=StandardResponse[DmOperationResp], summary="删除私信消息"
)
async def delete_messages(
    session: SessionDep, user: RequiredUser, req: DmDeleteReq
) -> StandardResponse[DmOperationResp]:
    """删除消息：只影响自己视角，对方仍能看到。"""
    msgkeys = [k for k in (_to_msgkey(m) for m in req.msgkeys) if k is not None]
    if not msgkeys:
        return StandardResponse(code=400, msg="msgkeys 不能为空或格式非法")
    affected = await DmService.delete_messages(session, user.mid, msgkeys)
    return StandardResponse(
        data=DmOperationResp(affected=affected, message=f"已删除 {affected} 条消息")
    )


@router.post(
    "/recall", response_model=StandardResponse[DmOperationResp], summary="撤回私信消息"
)
async def recall_message(
    session: SessionDep, user: RequiredUser, req: DmRecallReq
) -> StandardResponse[DmOperationResp]:
    """撤回消息：双方均不可见，并物理清除分片中的正文。

    仅发送者本人可撤回，且必须在配置的时间窗口内
    （时间判定直接取 msgkey 内嵌的时间戳，无需回查数据库）。
    """
    msgkey = _to_msgkey(req.msgkey)
    if msgkey is None:
        return StandardResponse(code=400, msg="msgkey 格式非法")
    ok, message = await DmService.recall_message(session, user.mid, msgkey)
    if not ok:
        return StandardResponse(
            code=400, msg=message, data=DmOperationResp(affected=0, message=message)
        )
    return StandardResponse(data=DmOperationResp(affected=1, message=message))


@router.post(
    "/ack", response_model=StandardResponse[DmOperationResp], summary="标记会话已读"
)
async def ack_session(
    session: SessionDep, user: RequiredUser, req: DmAckReq
) -> StandardResponse[DmOperationResp]:
    affected = await DmService.ack(
        session, user.mid, req.talker_mid, ack_msgkey=_to_msgkey(req.ack_msgkey)
    )
    return StandardResponse(
        data=DmOperationResp(
            affected=affected, message="已标记已读" if affected else "会话不存在"
        )
    )


@router.get("/unread", response_model=StandardResponse[int], summary="私信未读总数")
async def unread_dm(session: SessionDep, user: RequiredUser) -> StandardResponse[int]:
    return StandardResponse(data=await DmService.count_unread(session, user.mid))


__all__ = ["router"]
