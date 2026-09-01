"""原资源实时回捞（读取时补全标题 / 封面）。"""
from __future__ import annotations

import contextlib

from loguru import logger
from sqlalchemy.ext.asyncio.session import AsyncSession

from app.models.db import CommentIndex
from app.models.db.moment_tbl import TMoment
from bili_common.models.interaction import InteractionBizTypeEnum


async def _resolve_source_meta(
    session: AsyncSession,
    source_type: InteractionBizTypeEnum | None,
    source_id: str | None,
    biz_id: str | None = None,
    dyn_cache: dict[int, object] | None = None,
) -> tuple[str, str]:
    """按 source_type + source_id 实时回捞原资源的标题 / 封面。

    - DYNAMIC：直接读 TMoment（与事件表同库）；
    - COMMENT：先按 biz_id 取评论索引拿到 oid，再读 TMoment；
    - LOTTERY：走 RPA RPC 取详情（弱依赖，失败返回空）；
    - 其余类型（VIDEO / ARTICLE / OTHER）本地无原资源，返回空串。

    仅依赖 id，不在事件表冗余存储快照，省空间也更不易过期。

    注意：**不拼接跳转 uri**。业务由 ``business`` + ``type`` 表达，
    uri 由前端按这两个字段自行决定，后端不再产出链接。
    """
    st = source_type
    if isinstance(st, int):
        with contextlib.suppress(Exception):
            st = InteractionBizTypeEnum(st)

    if st is InteractionBizTypeEnum.DYNAMIC and source_id:
        dyn = await _load_dynamic(session, source_id, dyn_cache)
        if dyn is not None:
            return (dyn.contentText or "", _first_pic(dyn.contentJson) or "")

    if st is InteractionBizTypeEnum.COMMENT and biz_id:
        if biz_id.isdigit():
            idx = await session.get(CommentIndex, int(biz_id))
            if idx is not None and idx.oid:
                dyn = await _load_dynamic(session, str(idx.oid), dyn_cache)
                if dyn is not None:
                    return (dyn.contentText or "", _first_pic(dyn.contentJson) or "")
        return ("", "")

    if st is InteractionBizTypeEnum.LOTTERY and source_id:
        try:
            from app.services.infrastructure.rpa_rpc import rpa_rpc_client

            detail = await rpa_rpc_client.get_resource_detail(
                InteractionBizTypeEnum.LOTTERY.to_text(), source_id
            )
            if detail is not None:
                name = getattr(detail, "name", None) or ""
                cover = getattr(detail, "cover", None) or ""
                return (str(name), str(cover))
        except Exception:  # noqa: BLE001
            logger.debug("回捞 lottery 资源详情失败（弱依赖，忽略）", exc_info=True)
        return ("", "")

    return ("", "")


async def _load_dynamic(
    session: AsyncSession,
    dyn_id: str | None,
    dyn_cache: dict[int, object] | None,
) -> "TMoment | None":
    """按 dynId 读动态；软删动态视为不存在；带内存缓存避免一页内重复查。"""
    if not dyn_id or not str(dyn_id).isdigit():
        return None
    did = int(dyn_id)
    if dyn_cache is not None and did in dyn_cache:
        cached = dyn_cache[did]
        return cached if isinstance(cached, TMoment) else None
    dyn = await session.get(TMoment, did)
    if dyn_cache is not None:
        dyn_cache[did] = dyn
    if dyn is not None and getattr(dyn, "deletedAt", None) is not None:
        return None
    return dyn


def _first_pic(content_json: object) -> str:
    """从动态富文本正文里取第一张图片（外链图 / 资源卡封面）。"""
    if not content_json:
        return ""
    nodes: list = []
    if isinstance(content_json, dict):
        nodes = content_json.get("paragraphs", []) or content_json.get("nodes", [])
    elif isinstance(content_json, list):
        nodes = content_json
    for node in nodes:
        if not isinstance(node, dict):
            continue
        pic = node.get("picMeta")
        if isinstance(pic, dict) and pic.get("imgUrl"):
            return str(pic["imgUrl"])
        if node.get("cover"):
            return str(node["cover"])
    return ""
