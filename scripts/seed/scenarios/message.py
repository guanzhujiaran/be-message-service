"""场景④ 消息与管理：私信全流程 + 管理侧动作。

私信覆盖 a→b 与 b→a **双向互发**：每个方向均验证「发送 → 拉记录可见 → 撤回
（双方 RECALLED + ``recalled_by`` 落库与出参）→ 再发送 → 单方面删除
（自己不可见、对方仍可见）→ 删除后撤回被拒」；另对多对用户双向互发覆盖会话网络广度。

撤回 / 删除为**响亮断言**（暴露代码 bug）；业务拒绝（拉黑 / 陌生人过滤 / 超时）软降级跳过。
配对按「接收方是否已拉黑发送方」单向判定主动避让 ``blocked``（对齐
``FollowService.is_blocked_by``），保证断言不因历史黑名单整段失败。
"""
import asyncio

from loguru import logger
from tqdm import tqdm

from app.models.enums import DmMsgStatusEnum

from ..blocklist import _is_dm_blocked
from ..client import SeedClient
from ..probes import (
    _accept_stranger_dm,
    _dm_index_by_msgkey,
    _pick_dm_pair,
    _session_relation,
)
from .moderation import seed_moderation


async def seed_message(
    client: SeedClient,
    users: list[tuple[int, str | None]],
    count: int,
    dm_concurrency: int,
    blocked: set[tuple[int, int]] | None = None,
) -> None:
    """④ 消息与管理：私信（撤回/删除全流程）+ 通用计数 + 用户举报 + 封禁 + 头像审核流。

    ``blocked`` 为归一后的黑名单关系集合：私信配对按「接收方是否已拉黑发送方」
    主动避让——黑名单持久化会跨 seed 命中，而撤回 / 删除是**响亮断言**，
    一旦发送被拒整段场景直接失败。
    """
    if len(users) < 2:
        logger.warning("用户数不足，跳过消息与管理模块。")
        return
    blocked = blocked or set()

    # 1) 私信全流程：模拟真实用户「发送 → 确认可见 → 撤回（留记录）→
    #    再发送 → 单方面删除 → 删除后不可撤回」。
    a, b = await _pick_dm_pair(users, blocked)
    rel_b = await _session_relation(b[0], a[0])
    logger.info(
        f"私信对：{a[0]}<->{b[0]}（{b[0]} 视角会话关系={rel_b}，非 None 视为熟人可直达）"
    )

    # ---- 场景 A：发送 → 确认可见 → 撤回（留撤回记录）----
    try:
        msgkey1 = await client.dm_send(a[0], b[0], b[1])
        await client.approve_dm(msgkey1)
        await client.dm_ack(b[0], a[0])
        rows1 = await _dm_index_by_msgkey(int(msgkey1))
        if not rows1:
            logger.error(f"私信 {msgkey1} 发送后主库无索引行，终止私信场景。")
        else:
            owners1 = {r.owner_mid: r for r in rows1}
            assert a[0] in owners1, "发送方视角索引行缺失（写扩散未落库）"
            logger.info(
                f"私信已发送并落库：msgkey={msgkey1}，索引行 owner={sorted(owners1)}，"
                f"content_ready={sum(1 for r in rows1 if r.content_ready)}"
            )
            # 接收方拉取聊天记录确认可见（被陌生人过滤时无接收方视角，宽容跳过）
            msgs1 = await client.dm_messages(b[0], a[0])
            item1 = next(
                (it for it in msgs1.get("items", []) if it["msgkey"] == msgkey1), None
            )
            if item1 is None:
                logger.warning(
                    f"接收方 {b[0]} 视角未拉到 {msgkey1}（可能被陌生人过滤），跳过可见性断言。"
                )
            # 发送方在时间窗内撤回
            ok1, msg1 = await client.dm_recall(a[0], msgkey1)
            assert ok1, f"撤回应成功: {msg1}"
            # 查询数据库验证：双方索引行 RECALLED + 撤回记录（recalled_by/recalled_at）落库
            rows1b = await _dm_index_by_msgkey(int(msgkey1))
            assert rows1b and all(
                r.msg_status is DmMsgStatusEnum.RECALLED for r in rows1b
            ), "撤回后双方索引行应均为 RECALLED"
            assert all(
                r.recalled_by == a[0] for r in rows1b
            ), "撤回记录 recalled_by 应落库为撤回方"
            assert all(
                r.recalled_at is not None for r in rows1b
            ), "撤回记录 recalled_at 应落库"
            logger.success(
                f"私信撤回并留记录：msgkey={msgkey1}，recalled_by={rows1b[0].recalled_by}"
            )
            # 接收方拉取确认撤回记录出参（recalled_by / recalled_at）
            msgs1b = await client.dm_messages(b[0], a[0])
            item1b = next(
                (it for it in msgs1b.get("items", []) if it["msgkey"] == msgkey1), None
            )
            if item1b is not None:
                assert (
                    item1b["msg_status"] == DmMsgStatusEnum.RECALLED.value
                ), "撤回后状态应为 RECALLED"
                assert item1b.get("recalled_by") == a[0], "撤回记录应出参 recalled_by"
                assert item1b.get("recalled_at"), "撤回记录应出参 recalled_at"
                logger.success(f"撤回记录出参验证通过：recalled_by={item1b['recalled_by']}")
    except RuntimeError as e:
        # 拉黑/陌生人过滤/网络超时等业务拒绝 → 软降级跳过本场景（不中断整体）
        logger.warning(f"场景A 私信链路被拒（软降级）: {e}")

    # ---- 场景 B：单方面删除 → 删除后不可撤回 ----
    try:
        msgkey2 = await client.dm_send(a[0], b[0], b[1])
        await client.approve_dm(msgkey2)
        # 发送方单方面删除（仅自己视角，对方仍可见）
        await client.dm_delete(a[0], [msgkey2])
        rows2 = await _dm_index_by_msgkey(int(msgkey2))
        if not rows2:
            logger.error(f"私信 {msgkey2} 发送后主库无索引行，跳过删除场景。")
        else:
            state2 = {r.owner_mid: r.msg_status for r in rows2}
            assert state2.get(a[0]) is DmMsgStatusEnum.DELETED, "删除者视角应 DELETED"
            if b[0] in state2:
                assert (
                    state2[b[0]] is DmMsgStatusEnum.NORMAL
                ), "对方视角应保持 NORMAL（单方面删除）"
            logger.success(f"单方面删除验证通过：{a[0]}=DELETED，{b[0]}={state2.get(b[0])}")
            # 删除者自己看不到，对方仍可见
            my_msgs = await client.dm_messages(a[0], b[0])
            assert not any(
                it["msgkey"] == msgkey2 for it in my_msgs.get("items", [])
            ), "删除者视角不应再看到"
            other_msgs = await client.dm_messages(b[0], a[0])
            if b[0] in state2:
                assert any(
                    it["msgkey"] == msgkey2 for it in other_msgs.get("items", [])
                ), "对方视角应仍可见"
            # 删除后尝试撤回 → 应被拒（删除后不可撤回）
            ok2, msg2 = await client.dm_recall(a[0], msgkey2)
            assert not ok2 and "无法撤回" in msg2, f"删除后撤回应被拒绝: {ok2=} {msg2}"
            logger.success(f"删除后不可撤回验证通过：{msg2}")
    except RuntimeError as e:
        logger.warning(f"场景B 私信链路被拒（软降级）: {e}")

    # ---- 场景 C：反向互发（b→a）—— 私信写扩散双向覆盖 ----
    # 发送方换成 b（反向），验证「用户之间互相私信」链路在另一方向同样正确：
    # 发送 → 已读 → 撤回（recalled_by=b 落库 + 出参）→ 再发送 → 单方面删除 → 删除后不可撤回。
    try:
        msgkey3 = await client.dm_send(b[0], a[0], a[1])
        await client.approve_dm(msgkey3)
        await client.dm_ack(a[0], b[0])
        rows3 = await _dm_index_by_msgkey(int(msgkey3))
        if not rows3:
            logger.error(f"反向私信 {msgkey3} 发送后主库无索引行，跳过反向撤回场景。")
        else:
            owners3 = {r.owner_mid: r for r in rows3}
            assert b[0] in owners3 and a[0] in owners3, "反向发送写扩散应双方落库"
            logger.info(
                f"反向私信已发送并落库：msgkey={msgkey3}，索引行 owner={sorted(owners3)}，"
                f"content_ready={sum(1 for r in rows3 if r.content_ready)}"
            )
            # 发送方 b 在时间窗内撤回自己的消息
            ok3, msg3 = await client.dm_recall(b[0], msgkey3)
            assert ok3, f"反向撤回应成功: {msg3}"
            rows3b = await _dm_index_by_msgkey(int(msgkey3))
            assert rows3b and all(
                r.msg_status is DmMsgStatusEnum.RECALLED for r in rows3b
            ), "反向撤回后双方索引行应均为 RECALLED"
            assert all(
                r.recalled_by == b[0] for r in rows3b
            ), "反向撤回 recalled_by 应落库为发送方 b"
            assert all(
                r.recalled_at is not None for r in rows3b
            ), "反向撤回 recalled_at 应落库"
            # 接收方 a 拉取确认撤回记录出参
            msgs3b = await client.dm_messages(a[0], b[0])
            item3b = next(
                (it for it in msgs3b.get("items", []) if it["msgkey"] == msgkey3), None
            )
            if item3b is not None:
                assert (
                    item3b["msg_status"] == DmMsgStatusEnum.RECALLED.value
                ), "反向撤回后状态应为 RECALLED"
                assert item3b.get("recalled_by") == b[0], "反向撤回记录应出参 recalled_by"
            logger.success(
                f"反向互发撤回验证通过：b→a msgkey={msgkey3}，recalled_by={b[0]}"
            )

        # 反向 + 单方面删除 → 删除后不可撤回
        msgkey4 = await client.dm_send(b[0], a[0], a[1])
        await client.approve_dm(msgkey4)
        await client.dm_delete(b[0], [msgkey4])
        rows4 = await _dm_index_by_msgkey(int(msgkey4))
        if not rows4:
            logger.error(f"反向私信 {msgkey4} 发送后主库无索引行，跳过反向删除场景。")
        else:
            state4 = {r.owner_mid: r.msg_status for r in rows4}
            assert state4.get(b[0]) is DmMsgStatusEnum.DELETED, "反向删除者视角应 DELETED"
            if a[0] in state4:
                assert (
                    state4[a[0]] is DmMsgStatusEnum.NORMAL
                ), "反向对方视角应保持 NORMAL（单方面删除）"
            logger.success(f"反向单方面删除验证通过：{b[0]}=DELETED，{a[0]}={state4.get(a[0])}")
            # 删除者自己看不到，对方仍可见
            my_msgs4 = await client.dm_messages(b[0], a[0])
            assert not any(
                it["msgkey"] == msgkey4 for it in my_msgs4.get("items", [])
            ), "反向删除者视角不应再看到"
            other_msgs4 = await client.dm_messages(a[0], b[0])
            if a[0] in state4:
                assert any(
                    it["msgkey"] == msgkey4 for it in other_msgs4.get("items", [])
                ), "反向对方视角应仍可见"
            # 删除后尝试撤回 → 应被拒
            ok4, msg4 = await client.dm_recall(b[0], msgkey4)
            assert not ok4 and "无法撤回" in msg4, f"反向删除后撤回应被拒: {ok4=} {msg4}"
            logger.success(f"反向删除后不可撤回验证通过：{msg4}")
    except RuntimeError as e:
        # 拉黑/陌生人过滤/网络超时等业务拒绝 → 软降级跳过反向场景（不中断整体）
        logger.warning(f"场景C 反向私信链路被拒（软降级）: {e}")

    # ---- 场景 D：多对用户互相私信（广度）—— 会话网络覆盖 ----
    # 深度撤回/删除已由 a/b 对（场景 A/B/C）承担；此处让更多用户对**双向互发**，
    # 覆盖「用户之间互相私信」的会话网络广度：每对 发送 → 审核 → 已读 → 双方可见。
    # 任一方关闭陌生人私信 / 可见性未确认 → 宽容跳过该对，不阻断整体流程。
    pair_count = 0
    for i in range(0, min(len(users) - 1, 8), 2):
        x, y = users[i], users[i + 1]
        if (x[0] == a[0] and y[0] == b[0]) or (x[0] == b[0] and y[0] == a[0]):
            continue  # a/b 对已深度覆盖，跳过避免重复
        if _is_dm_blocked(blocked, x[0], y[0]) or _is_dm_blocked(blocked, y[0], x[0]):
            logger.warning(f"用户对 {x[0]}<->{y[0]} 存在黑名单关系，跳过该对。")
            continue
        if not await _accept_stranger_dm(y[0]) or not await _accept_stranger_dm(x[0]):
            logger.warning(
                f"用户对 {x[0]}<->{y[0]} 存在关闭陌生人私信，跳过该对。"
            )
            continue
        # x→y 与 y→x 双向互发（拉黑/陌生人过滤等业务拒绝 → 跳过该对）
        try:
            mk_xy = await client.dm_send(x[0], y[0], y[1])
            await client.approve_dm(mk_xy)
            await client.dm_ack(y[0], x[0])
            mk_yx = await client.dm_send(y[0], x[0], x[1])
            await client.approve_dm(mk_yx)
            await client.dm_ack(x[0], y[0])
        except RuntimeError as e:
            logger.warning(
                f"用户对 {x[0]}<->{y[0]} 互发被拒（可能拉黑），跳过该对: {e}"
            )
            continue
        # 双方视角可见性（宽容：被陌生人过滤时跳过该对）
        xy_visible = any(
            it["msgkey"] == mk_xy
            for it in (await client.dm_messages(x[0], y[0])).get("items", [])
        )
        yx_visible = any(
            it["msgkey"] == mk_yx
            for it in (await client.dm_messages(y[0], x[0])).get("items", [])
        )
        if not (xy_visible and yx_visible):
            logger.warning(
                f"用户对 {x[0]}<->{y[0]} 互发可见性未完全确认（可能被陌生人过滤），宽容跳过。"
            )
            continue
        pair_count += 1
        logger.success(
            f"用户对 {x[0]}<->{y[0]} 双向互发可见：x→y={mk_xy}，y→x={mk_yx}"
        )
    if pair_count:
        logger.success(f"[消息与管理] 额外 {pair_count} 对用户完成互相私信")
    else:
        logger.warning("[消息与管理] 无额外用户对完成互发（用户数不足或均被陌生人过滤）")

    # ---- 场景 E：批量互发私信填充（O(n²) 遍历用户对，总计 --full-count 条）----
    # O(n²) 遍历所有有序用户对 (sender, receiver)，把**总计 count 条**配额尽量分散
    # 到不同用户对上（每对 1 条，超过 count 对则取前 count 对），而不是把 count 条
    # 全发给同一个随机用户——这样既覆盖「用户对网络」的广度，发送量又受控为 count 条。
    # 业务拒绝（拉黑 / 陌生人过滤等）软降级跳过该对，不中断整体。
    if count > 0 and len(users) >= 2:
        sem = asyncio.Semaphore(dm_concurrency)

        async def _send_one_directed(sender, receiver) -> bool:
            """单向发 1 条并审核通过；业务拒绝返回 False（不报 warning）。"""
            async with sem:
                try:
                    mk = await client.dm_send(sender[0], receiver[0], receiver[1])
                    await client.approve_dm(mk)
                    return True
                except Exception as e:  # noqa: BLE001
                    msg = str(e)
                    if any(k in msg for k in ("拉黑", "黑名单", "陌生", "对方已")):
                        logger.info(
                            f"批量私信 {sender[0]}→{receiver[0]} 被业务规则拒绝（预期内跳过）: {msg}"
                        )
                    else:
                        logger.warning(
                            f"批量私信 {sender[0]}→{receiver[0]} 发送失败（跳过）: {e}"
                        )
                    return False

        # O(n²) 遍历全部有序用户对（剔除「接收方已拉黑发送方」的必拒方向），
        # 取前 count 对——每对只发 1 条，故总发送量 = min(count, 可发对数)
        directed = [
            (sender, receiver)
            for sender in users
            for receiver in users
            if sender[0] != receiver[0]
            and not _is_dm_blocked(blocked, sender[0], receiver[0])
        ][:count]

        total = 0
        if directed:
            pending = [
                asyncio.create_task(_send_one_directed(s, r)) for s, r in directed
            ]
            for f in tqdm(
                asyncio.as_completed(pending),
                total=len(pending),
                desc="seed dm bulk",
            ):
                try:
                    if await f:
                        total += 1
                except Exception:  # noqa: BLE001
                    pass
        logger.success(
            f"[消息与管理] 批量私信填充完成：O(n²) 遍历用户对取前 {len(directed)} 对"
            f"（每对 1 条），实际发送 {total} 条（目标 {count} 条）"
        )


    await seed_moderation(client, users)
    logger.success("[消息与管理] 私信/通用计数/举报/封禁/头像审核流已覆盖")
