"""落库断言：动态 / 评论 @ 渲染与 AT 事件触达、转发计数状态机。

全互动阶段的断言失败会「响亮报错」（``logger.error``）以暴露代码 bug；
@ 通知属弱依赖（黑名单静默 / 消息设置闸门 / 幂等去重）只 warning。
"""
from bili_common.models import InteractionActionTypeEnum, InteractionBizTypeEnum
from loguru import logger

from .client import SeedClient


async def _verify_at_event(client: SeedClient, at_mid: int, resource_id: str) -> None:
    """查被 @ 用户的 AT 事件提醒，确认存在指向 ``resource_id``（评论 rpid / 动态 dynId）的通知。

    @ 通知是**弱依赖**，以下情况都会导致查不到，均属预期、只 warning 不判定失败：
    ① 黑名单静默——@ 本身允许，但被 @ 者与发布者存在任一向黑名单关系时不投递提醒（2.50.0）；
    ② 消息设置闸门（用户关闭 @ 提醒）；③ 幂等去重。
    """
    try:
        data = await client.event_list(at_mid, InteractionActionTypeEnum.AT)
    except RuntimeError as e:
        logger.warning(f"[@通知] 被@用户 {at_mid} 的 AT 事件列表查询失败: {e}")
        return
    items = ((data.get("total") or {}).get("items")) or []
    hit = next(
        (
            it
            for it in items
            if str((it.get("item") or {}).get("resource_id")) == resource_id
        ),
        None,
    )
    if hit is None:
        logger.warning(
            f"[@通知] 被@用户 {at_mid} 的 AT 提醒中未找到 resource_id={resource_id}"
            f"（共 {len(items)} 条；可能是黑名单静默 / 消息设置闸门 / 幂等去重，均属预期）"
        )
        return
    logger.success(f"[@通知] 验证通过：用户 {at_mid} 已收到 resource_id={resource_id} 的 @ 提醒")


async def _verify_dynamic_at(client: SeedClient, viewer_mid: int, dyn_id: int) -> None:
    """回查动态详情，验证正文末尾追加的随机 @ 已落库并渲染。

    断言点：① desc 模块回显 AT 节点（`type=AT` + `bizId`/`name`）；
    ② 正文 `text` 中 AT 节点昵称已渲染为 `@昵称`；③ 被 @ 用户收到 AT 事件（弱依赖）。
    """
    try:
        data = await client.detail(viewer_mid, dyn_id)
    except RuntimeError as e:
        logger.error(f"[动态@] 详情回查失败 dyn={dyn_id}: {e}")
        return
    texts: list[str] = []
    at_nodes: list[dict] = []
    for m in data.get("modules") or []:
        if m.get("text"):
            texts.append(str(m["text"]))
        at_nodes.extend(n for n in (m.get("nodes") or []) if n.get("type") == "AT")
    if not at_nodes:
        logger.error(
            f"[动态@] dyn={dyn_id} 详情未回显任何 AT 节点（正文={texts[:1]}），动态 @ 链路未生效"
        )
        return
    joined = "".join(texts)
    missed = [
        n.get("name") for n in at_nodes if n.get("name") and f"@{n['name']}" not in joined
    ]
    if missed:
        logger.error(f"[动态@] dyn={dyn_id} 正文未渲染 @昵称: {missed}")
        return
    names = [n.get("name") for n in at_nodes]
    logger.success(
        f"[动态@] 验证通过 dyn={dyn_id}：AT 节点 {len(at_nodes)} 个（{names}）已渲染进正文"
    )
    first_mid = at_nodes[0].get("bizId")
    if first_mid:
        await _verify_at_event(client, int(first_mid), str(dyn_id))


async def _verify_repost_count(
    client: SeedClient, viewer_mid: int, pairs: list[tuple[int, int]]
) -> None:
    """断言「转发过审 → 源动态 ``repostCount`` +1」状态机触发点已生效。

    取第一对 (源动态, 转发动态) 回查互动态计数：计数为 0 说明审核通过时
    源动态计数未累加（状态机触发点 ⑥ 未生效），响亮报错。
    """
    if not pairs:
        return
    src_dyn, fwd_dyn = pairs[0]
    try:
        data = await client.browse(viewer_mid, src_dyn)
    except RuntimeError as e:
        logger.error(f"[转发计数] 源动态 dyn={src_dyn} 互动态回查失败: {e}")
        return
    count = int(data.get("repostCount") or 0)
    if count <= 0:
        logger.error(
            f"[转发计数] 源动态 dyn={src_dyn} repostCount={count}"
            f"（转发 dyn={fwd_dyn} 已过审），转发计数状态机未生效"
        )
        return
    logger.success(
        f"[转发计数] 验证通过：源动态 dyn={src_dyn} repostCount={count}（转发 dyn={fwd_dyn}）"
    )


async def _verify_comment_at(
    client: SeedClient,
    viewer_mid: int,
    oid: int,
    *,
    biz_type: InteractionBizTypeEnum = InteractionBizTypeEnum.DYNAMIC,
) -> None:
    """回查一级评论列表，验证评论正文末尾追加的随机 @ 已落库并渲染。

    断言点：① 出参带 ``at_name_to_mid`（昵称 → mid）/ ``at_users`（被@用户快照）；
    ② 正文 `message` 中 `@{mid}` 占位符已渲染回 `@昵称`；③ 被 @ 用户收到 AT 事件（弱依赖）。
    """
    try:
        data = await client.comment_main(viewer_mid, oid, biz_type=biz_type)
    except RuntimeError as e:
        logger.error(f"[评论@] 评论列表回查失败 {biz_type}={oid}: {e}")
        return
    items = data.get("items") or []
    hit = [it for it in items if it.get("at_name_to_mid") or it.get("at_users")]
    if not hit:
        logger.error(
            f"[评论@] {biz_type}={oid} 的 {len(items)} 条一级评论均无 @ 落库，评论 @ 链路未生效"
        )
        return
    bad: list[tuple] = []
    for it in hit:
        msg = str(it.get("message") or "")
        for uname in it.get("at_name_to_mid") or {}:
            if f"@{uname}" not in msg:
                bad.append((it.get("rpid"), uname, msg))
    if bad:
        logger.error(f"[评论@] 正文未渲染 @昵称（rpid, 昵称, 正文）: {bad[:3]}")
        return
    names = sorted({u for it in hit for u in (it.get("at_name_to_mid") or {})})
    logger.success(
        f"[评论@] 验证通过 {biz_type}={oid}：{len(hit)}/{len(items)} 条一级评论带 @，被@昵称 {names[:5]}"
    )
    # AT 通知以评论 rpid 为 biz_id（resource_id）
    first_name_to_mid = hit[0].get("at_name_to_mid") or {}
    if first_name_to_mid:
        await _verify_at_event(
            client, next(iter(first_name_to_mid.values())), str(hit[0].get("rpid"))
        )
