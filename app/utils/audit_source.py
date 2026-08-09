"""审核项「内容来源」构造工具。

把后端的 `(oid, type)` / `session_key` 翻译成前端可直接跳转的站内路径，
路径与 `Vue3FrontEndDemoExercise/src/router/index.ts` 中的路由一一对应：

| 评论区类型 | 站内落地页                                   |
|-----------|---------------------------------------------|
| lottery   | `/app/lot-data/card-detail?id={oid}`         |
| feedback  | `/app/feedback`                              |
| dynamic   | 无站内页，给出 B 站动态外链                    |
| article   | 无站内页，给出 B 站专栏外链                    |
| other     | 无                                           |

私信没有「内容实体」，来源即会话本身，跳转到私信审核页的会话上下文抽屉
（`/app/admin/message-dm?session_key=...&msgkey=...`，前端据此自动打开上下文）。

前端路由一旦调整，只需要改这里，不必动业务代码。
"""

from app.models.enums import CommentTypeEnum
from app.models.schemas.audit import AuditSourceInfo

# 评论区类型 → 展示名
_COMMENT_TYPE_LABEL: dict[str, str] = {
    CommentTypeEnum.DYNAMIC: "动态",
    CommentTypeEnum.ARTICLE: "专栏",
    CommentTypeEnum.LOTTERY: "抽奖卡片",
    CommentTypeEnum.FEEDBACK: "反馈区",
    CommentTypeEnum.OTHER: "其他",
}


def build_comment_source(
    oid: int,
    type_: CommentTypeEnum,
    rpid: int,
    up_mid: int | None = None,
) -> AuditSourceInfo:
    """构造一条评论的内容来源（评论区 + 跳转地址）。"""
    biz_type = str(type_)
    name = _COMMENT_TYPE_LABEL.get(biz_type, "其他")
    params = {"rpid": str(rpid), "oid": str(oid), "type": biz_type}

    url: str | None = None
    external_url: str | None = None
    label = f"{name} #{oid}"

    if type_ == CommentTypeEnum.LOTTERY:
        url = f"/app/lot-data/card-detail?id={oid}&rpid={rpid}"
    elif type_ == CommentTypeEnum.FEEDBACK:
        url = f"/app/feedback?rpid={rpid}"
        label = "反馈区"
    elif type_ == CommentTypeEnum.DYNAMIC:
        external_url = f"https://t.bilibili.com/{oid}"
    elif type_ == CommentTypeEnum.ARTICLE:
        external_url = f"https://www.bilibili.com/read/cv{oid}"

    return AuditSourceInfo(
        kind="comment",
        biz_type=biz_type,
        label=label,
        oid=str(oid),
        up_mid=up_mid,
        url=url,
        external_url=external_url,
        params=params,
    )


def build_dm_source(
    session_key: str,
    sender_mid: int,
    talker_mid: int,
    msgkey: int,
) -> AuditSourceInfo:
    """构造一条私信的内容来源（会话 + 上下文跳转地址）。"""
    return AuditSourceInfo(
        kind="dm",
        biz_type="dm",
        label=f"私信会话 {sender_mid} → {talker_mid}",
        oid=None,
        up_mid=None,
        url=f"/app/admin/message-dm?session_key={session_key}&msgkey={msgkey}",
        external_url=None,
        params={
            "session_key": session_key,
            "msgkey": str(msgkey),
            "sender_mid": str(sender_mid),
            "talker_mid": str(talker_mid),
        },
    )


__all__ = ["build_comment_source", "build_dm_source"]
