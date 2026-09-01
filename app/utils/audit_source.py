"""审核项「内容来源」构造工具。

把后端的 `(oid, type)` / `session_key` 翻译成**前端路由名跳转目标**：
跳转目标统一由 :func:`app.utils.route_target.build_route_target` 生成，路由名取自
:class:`app.models.enums.FrontendRouteEnum`（值 = 前端路由的 `name`），
**后端不再拼任何站内路径**（见计划书 §2.10）：

| 评论区类型 | 跳转目标（`route:{name}?{query}`）            |
|-----------|-----------------------------------------------|
| lottery   | `route:抽奖卡片详情?id={oid}&rpid={rpid}`       |
| dynamic   | 无站内落地页，给出 B 站动态外链                  |

私信没有「内容实体」，来源即会话本身，跳转到私信审核页的会话上下文抽屉
（`route:ADMIN_MESSAGE_DM?session_key=...&msgkey=...`，前端据此自动打开上下文）。

展示名一律取 `app.models.biz_type.comment_type_label()`（biz_type 单一真相源，计划书 §5.9），
前端路由改名 / 改路径只需同步 `FrontendRouteEnum`，不必动业务代码。
"""

from app.models.biz_type import comment_type_label, comment_type_to_biz_type
from app.models.enums import FrontendRouteEnum, InteractionBizTypeEnum
from app.models.schemas.audit import AuditSourceInfo
from app.utils.route_target import build_route_target


def build_comment_source(
    oid: int,
    type_: InteractionBizTypeEnum,
    rpid: int,
    up_mid: int | None = None,
) -> AuditSourceInfo:
    """构造一条评论的内容来源（评论区 + 跳转地址）。"""
    biz = comment_type_to_biz_type(type_)
    # 有对应 biz_type 时出参 biz_type 用对外文字（lottery / dynamic），无对应时回落评论区类型数值
    biz_type = biz.to_text() if biz else str(int(type_))
    name = comment_type_label(type_)

    url: str | None = None
    external_url: str | None = None
    label = f"{name} #{oid}"

    if type_ == InteractionBizTypeEnum.LOTTERY:
        url = build_route_target(
            FrontendRouteEnum.LOTTERY_CARD_DETAIL, {"id": oid, "rpid": rpid}
        )
    elif type_ == InteractionBizTypeEnum.DYNAMIC:
        external_url = f"https://t.bilibili.com/{oid}"

    return AuditSourceInfo(
        kind="comment",
        biz_type=biz_type,
        label=label,
        oid=str(oid),
        up_mid=up_mid,
        url=url,
        external_url=external_url,
        params={"rpid": str(rpid), "oid": str(oid), "type": biz_type},
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
        url=build_route_target(
            FrontendRouteEnum.ADMIN_MESSAGE_DM,
            {"session_key": session_key, "msgkey": msgkey},
        ),
        external_url=None,
        params={
            "session_key": session_key,
            "msgkey": str(msgkey),
            "sender_mid": str(sender_mid),
            "talker_mid": str(talker_mid),
        },
    )


__all__ = ["build_comment_source", "build_dm_source"]
