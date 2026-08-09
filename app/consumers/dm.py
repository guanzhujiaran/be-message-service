"""私信相关的 MQ 消费处理。

`handle_dm_content` 是「内容异步化」的落地端：把正文写入 msgkey 路由到的
月度库分表，成功后回写索引行的 `content_ready`。

写失败时**不 requeue**（避免坏消息无限打转），而是转入死信表由定时任务补偿，
这样既不阻塞队列，也不会丢正文。

注意：私信的「送达」由写扩散（收发双方各写索引 / 会话行）保证，属于站内信，
**不向第三方推送渠道发消息**；到达提醒也只体现在站内信未读计数上，由前端轮询
`msg_feed` / `heartbeat` 感知，无需经 PushMe / PushPlus 等渠道。
"""

from faststream.rabbit import RabbitMessage
from loguru import logger

from app.core.database import new_session
from app.models.db import DmContentDeadLetter
from app.models.schemas import DmContentPayload
from app.services.dm import DmService
from app.services.dm_content import DmContentService


async def handle_dm_content(payload: DmContentPayload, msg: RabbitMessage) -> None:
    """消费私信正文，写入分库分表。"""
    ok = await DmContentService.write(payload)
    async with new_session() as session:
        if ok:
            await DmService.mark_content_ready(session, payload.msgkey)
        else:
            session.add(
                DmContentDeadLetter(
                    msgkey=payload.msgkey,
                    session_key=payload.session_key,
                    sender_uid=payload.sender_uid,
                    receiver_uid=payload.receiver_uid,
                    msg_type=payload.msg_type,
                    content=payload.content,
                    msg_ts=payload.msg_ts,
                    last_error="消费者写入分片失败",
                )
            )
            try:
                await session.commit()
            except Exception as e:  # noqa: BLE001
                # 唯一索引冲突说明死信已存在，忽略即可
                await session.rollback()
                logger.debug(f"死信写入跳过 msgkey={payload.msgkey}: {e}")
    # 无论成功与否都 ack：失败路径已经落死信，重投只会浪费队列
    await msg.ack()


__all__ = ["handle_dm_content"]
