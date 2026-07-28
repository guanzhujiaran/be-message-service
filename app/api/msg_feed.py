"""消息系统（message-service）聚合路由。

将各功能模块（当前为「推送」push，后续会加入评论 / 对话 / 私信等）统一挂载在
/api/v1/message 前缀之下，便于前端 / nodejs-pptr 代理统一转发。
"""

from fastapi import APIRouter


router = APIRouter(prefix="/api/v1/message/msg_feed", tags=["message"])
# TODO:后续添加其它方法