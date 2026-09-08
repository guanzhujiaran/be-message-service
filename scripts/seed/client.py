"""``SeedClient``：以指定用户身份调 be-message HTTP 接口的薄封装。

按功能域分段：动态 / 评论 / 收藏夹 / 关注·拉黑 / 事件通知 / 私信 / 举报·封禁 / 头像审核。
新增资源的接口方法请加在对应分段内，保持一处收口。
"""
import asyncio

import httpx
from bili_common.models import InteractionActionTypeEnum, InteractionBizTypeEnum
from loguru import logger

from app.models.enums import BanDurationTypeEnum, NotifyTargetTypeEnum

from .config import (
    _BAN_SERVICES,
    _REPORT_REASON_TYPE,
    _SEED_HTTP_TIMEOUT,
    _SEED_REQ_CONCURRENCY,
    _SEED_REQ_TIMEOUT,
)
from .helpers import _at_name_to_mid, _at_text_suffix, _headers
from .material import _COMMENTS
from .rr import _rr


class SeedClient:
    """薄封装：以指定用户身份调 be-message HTTP 接口。"""

    def __init__(
        self,
        base_url: str,
        admin_mid: int,
        req_concurrency: int = _SEED_REQ_CONCURRENCY,
    ) -> None:
        self.base = base_url.rstrip("/")
        self.admin_mid = admin_mid
        # 默认 90s：覆盖雪花 ID 分钟级等待（≤60s）+ 处理余量，可用 SEED_HTTP_TIMEOUT 覆盖
        self.client = httpx.AsyncClient(timeout=_SEED_HTTP_TIMEOUT)
        # 请求级全局信号量：限制同时在途 HTTP 数，避免打满服务端 MySQL 连接池
        # （pool_size + max_overflow，运行时常见 20+30=50）触发 QueuePool 耗尽 500。
        self._req_sem = asyncio.Semaphore(max(1, req_concurrency))

    async def __aenter__(self) -> "SeedClient":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.client.aclose()

    async def _post(
        self, path: str, mid: int, body: dict, *, role: str = "normal"
    ) -> dict:
        async with self._req_sem:
            resp = await self.client.post(
                f"{self.base}{path}", json=body, headers=_headers(mid, role=role)
            )
            if resp.status_code != 200:
                raise RuntimeError(
                    f"POST {path} 非 200: {resp.status_code} {resp.text[:300]}"
                )
            payload = resp.json()
            if payload.get("code", 0) != 0:
                raise RuntimeError(f"POST {path} 业务失败: {payload}")
            return payload.get("data") or {}

    async def _get(
        self, path: str, mid: int, params: dict | None = None, *, role: str = "normal"
    ) -> dict:
        async with self._req_sem:
            resp = await self.client.get(
                f"{self.base}{path}",
                params=params,
                headers=_headers(mid, role=role),
            )
            if resp.status_code != 200:
                raise RuntimeError(
                    f"GET {path} 非 200: {resp.status_code} {resp.text[:300]}"
                )
            payload = resp.json()
            if payload.get("code", 0) != 0:
                raise RuntimeError(f"GET {path} 业务失败: {payload}")
            return payload.get("data") or {}

    async def _req(self, factory, label: str, *, retries: int = 3) -> dict:
        """执行一个请求：``factory()`` 返回协程，超时/连接错误自动重试。

        覆盖 asyncio 超时与 httpx 传输层错误（含 RemoteProtocolError /
        ReadError / ConnectError 等「服务端断开连接」类异常），避免批量灌数时
        偶发连接重置导致整个任务崩溃。
        """
        last: Exception | None = None
        for attempt in range(1, retries + 1):
            try:
                # 总超时默认 120s：覆盖雪花 ID 分钟级等待（≤60s）+ 重试余量，
                # 避免跨分钟边界时 30s/60s 级别的超时误报，可用 SEED_REQ_TIMEOUT 覆盖
                return await asyncio.wait_for(factory(), timeout=_SEED_REQ_TIMEOUT)
            except (TimeoutError, httpx.TransportError) as e:
                last = e
                if attempt < retries:
                    logger.warning(
                        f"{label} 第 {attempt} 次失败({type(e).__name__})，重试…"
                    )
                    await asyncio.sleep(1.0)
        raise RuntimeError(f"请求失败(×{retries}): {label}: {last}")

    # ==================== 动态体系 ====================

    async def create_topic(self, mid: int, name: str) -> int:
        """创建话题并返回 topicId（创建即 auditing）。"""
        data = await self._req(
            lambda: self._post(
                "/api/v1/community/topic/create",
                mid,
                {"topicName": name, "topicDesc": f"{name} 话题（seed 生成）"},
            ),
            f"topic create mid={mid}",
        )
        return int(data["topicId"])

    async def approve_topic(self, topic_id: int) -> None:
        """管理员审核话题通过（→ normal）。"""
        await self._req(
            lambda: self._post(
                "/api/v1/community/topic/audit/approve",
                self.admin_mid,
                {"topicId": topic_id, "remark": "SEED approve"},
                role="root",
            ),
            f"topic approve id={topic_id}",
        )

    async def list_my_topics(
        self, page: int = 1, page_size: int = 50
    ) -> dict:
        """分页获取当前登录用户（admin）创建的话题，返回 {items, hasMore}。"""
        return await self._req(
            lambda: self._get(
                "/api/v1/community/topic/mine",
                self.admin_mid,
                {"page": page, "page_size": page_size},
            ),
            f"topic mine page={page}",
        )

    async def create_dynamic(
        self, mid: int, *, scene: str, content: list[dict], topic_id: int | None = None
    ) -> int:
        body: dict = {"scene": scene, "content": content}
        if topic_id is not None:
            body["topic"] = {"topicId": topic_id}
        data = await self._req(
            lambda: self._post("/api/v1/community/create", mid, body),
            f"create mid={mid}",
        )
        return int(data["dynId"])

    async def approve(self, dyn_id: int) -> None:
        await self._req(
            lambda: self._post(
                "/api/v1/community/audit/approve",
                self.admin_mid,
                {"dynId": dyn_id, "remark": "SEED script approve"},
                role="root",
            ),
            f"approve dyn={dyn_id}",
        )

    async def repost(self, mid: int, src_dyn_id: int, content: list[dict]) -> int:
        data = await self._req(
            lambda: self._post(
                "/api/v1/community/repost",
                mid,
                {"srcDynId": src_dyn_id, "content": content},
            ),
            f"repost mid={mid} src={src_dyn_id}",
        )
        return int(data["dynId"])

    async def thumb(self, mid: int, dyn_id: int) -> None:
        await self._req(
            lambda: self._post(
                "/api/v1/community/thumb",
                mid,
                {"bizType": InteractionBizTypeEnum.DYNAMIC, "bizId": dyn_id, "up": 1},
            ),
            f"thumb mid={mid} dyn={dyn_id}",
        )

    async def thumb_lottery(self, mid: int, lottery_id: int) -> None:
        """对 lottery 资源点赞（TInteractionStat 通用计数；RPC 校验失败降级放行）。"""
        try:
            await self._req(
                lambda: self._post(
                    "/api/v1/community/thumb",
                    mid,
                    {"bizType": InteractionBizTypeEnum.LOTTERY, "bizId": lottery_id, "up": 1},
                ),
                f"thumb lottery mid={mid} id={lottery_id}",
            )
        except RuntimeError as e:
            logger.warning(f"lottery 点赞失败（RPC 校验或资源不存在）: {e}")

    async def browse(self, mid: int, dyn_id: int) -> dict:
        """详情浏览触发（detail 接口兼作浏览统计触发点，投递浏览 MQ）。

        出参为该资源的互动计数（`repostCount` 等），供转发链路断言源动态计数 +1。
        """
        return await self._req(
            lambda: self._get(
                f"/api/v1/community/interaction/status/{dyn_id}",
                mid,
                {"bizType": InteractionBizTypeEnum.DYNAMIC},
            ),
            f"browse mid={mid} dyn={dyn_id}",
        )

    async def create_forward(
        self,
        mid: int,
        src_dyn_id: int,
        content: list[dict],
        topic_id: int | None = None,
    ) -> int:
        """走 `POST /create`（scene=FORWARD）转发——与 `/repost` 是两条独立实现，需双覆盖。

        该分支支持挂话题（`topics`），`/repost` 不支持，故 seed 两条路径都跑。
        """
        body: dict = {
            "scene": "FORWARD",
            "content": content,
            "repostSrc": {"dynId": src_dyn_id},
        }
        if topic_id is not None:
            body["topic"] = {"topicId": topic_id}
        data = await self._req(
            lambda: self._post("/api/v1/community/create", mid, body),
            f"create FORWARD mid={mid} src={src_dyn_id}",
        )
        return int(data["dynId"])

    async def detail(self, mid: int, dyn_id: int) -> dict:
        """动态详情（desc 模块含 `text` 正文与 `nodes` 富文本节点，用于验证 @）。"""
        return await self._req(
            lambda: self._get(f"/api/v1/community/detail/{dyn_id}", mid),
            f"detail mid={mid} dyn={dyn_id}",
        )

    async def report_moment(self, mid: int, dyn_id: int) -> None:
        # 2.41.0：统一举报接口泛化为 bizType+bizId（不再用 dynId）
        await self._req(
            lambda: self._post(
                "/api/v1/report",
                mid,
                {
                    "bizType": InteractionBizTypeEnum.DYNAMIC,
                    "bizId": dyn_id,
                    "reasonType": _REPORT_REASON_TYPE,
                    "reasonDesc": "seed 举报动态",
                },
            ),
            f"report moment mid={mid} dyn={dyn_id}",
        )

    async def top_moment(self, mid: int, dyn_id: int) -> None:
        await self._req(
            lambda: self._post("/api/v1/community/space/top", mid, {"dynId": dyn_id}),
            f"top moment mid={mid} dyn={dyn_id}",
        )

    # ==================== 评论体系 ====================

    async def add_comment(
        self,
        mid: int,
        dyn_id: int,
        author_mid: int,
        *,
        biz_type: InteractionBizTypeEnum = InteractionBizTypeEnum.DYNAMIC,
        root: str = "0",
        parent: str = "0",
        message: str | None = None,
        at_mids: list[int] | None = None,
        at_name_to_mid: dict[str, int] | None = None,
        at_users: list[tuple[int, str]] | None = None,
    ) -> str:
        """发评论并返回 rpid（评论开启先审后发，正常落 auditing）。

        ``at_users`` 为随机 @ 目标 ``[(mid, 昵称)]``：以 ``@昵称`` 追加到正文末尾，
        并同时带上 ``at_mids`` / ``at_name_to_mid``（服务端归一为 ``@{mid}`` 占位符）。
        """
        # 正文唯一化：评论服务对「同用户同正文 10s 内 >3 次」限流（Phase 2.6），
        # 末尾追加轮遍 @，昵称组合各异，兼作正文去重（md5 key 唯一）；
        # 正文素材取自大素材池（biliopusdb 去重后）轮遍取，不重复。
        raw_message = message or _rr.pick(_COMMENTS)
        targets = list(at_users or [])
        body: dict = {
            "oid": str(dyn_id),
            "type": biz_type,
            "root": root,
            "parent": parent,
            "message": f"{raw_message}{_at_text_suffix(targets)}",
            "up_mid": author_mid,
        }
        # @ 目标并入显式传入的 at_mids / at_name_to_mid（显式 @ 场景二者共存）
        merged_mids = list(at_mids or [])
        merged_mids.extend(mid for mid, _ in targets if mid not in merged_mids)
        merged_name_to_mid = dict(at_name_to_mid or {})
        merged_name_to_mid.update(_at_name_to_mid(targets))
        if merged_mids:
            body["at_mids"] = merged_mids
        if merged_name_to_mid:
            body["at_name_to_mid"] = merged_name_to_mid
        data = await self._req(
            lambda: self._post("/api/v1/comment/add", mid, body),
            f"comment mid={mid} dyn={dyn_id}",
        )
        return data.get("rpid") or ""

    async def approve_comment(self, rpid: str) -> None:
        """管理员审核通过一条评论（op=pass，root）→ NORMAL 对外可见。"""
        if not rpid:
            return
        await self._req(
            lambda: self._post(
                "/api/v1/comment/admin/audit",
                self.admin_mid,
                {"rpid": rpid, "op": "pass"},
                role="root",
            ),
            f"comment approve rpid={rpid}",
        )

    async def comment_action(self, mid: int, rpid: str, action: int) -> None:
        """评论点赞(1) / 点踩(2)。"""
        await self._req(
            lambda: self._post(
                "/api/v1/comment/action", mid, {"rpid": rpid, "action": action}
            ),
            f"comment action mid={mid} rpid={rpid}",
        )

    async def report_comment(self, mid: int, rpid: str) -> None:
        await self._req(
            lambda: self._post(
                "/api/v1/comment/report",
                mid,
                {
                    "rpid": rpid,
                    "reasonType": _REPORT_REASON_TYPE,
                    "reasonDesc": "seed 举报评论",
                },
            ),
            f"comment report mid={mid} rpid={rpid}",
        )

    async def top_comment(
        self,
        mid: int,
        oid: int,
        rpid: str,
        *,
        biz_type: InteractionBizTypeEnum = InteractionBizTypeEnum.DYNAMIC,
    ) -> None:
        """评论置顶（评论区 up_mid=资源作者；lottery 等通用资源同链路）。"""
        await self._req(
            lambda: self._post(
                "/api/v1/comment/top",
                mid,
                {"oid": str(oid), "type": biz_type, "rpid": rpid, "top": True},
            ),
            f"comment top mid={mid} oid={oid} type={biz_type} rpid={rpid}",
        )

    async def comment_main(
        self,
        mid: int,
        oid: int,
        page_size: int = 20,
        *,
        biz_type: InteractionBizTypeEnum = InteractionBizTypeEnum.DYNAMIC,
    ) -> dict:
        """一级评论列表（用于验证 @ 落库与 `@{mid}` → `@昵称` 渲染）。"""
        return await self._req(
            lambda: self._get(
                "/api/v1/comment/main",
                mid,
                {
                    "oid": str(oid),
                    "type": biz_type,
                    "page_size": page_size,
                },
            ),
            f"comment main mid={mid} oid={oid} type={biz_type}",
        )

    async def event_list(
        self, mid: int, event_type: InteractionActionTypeEnum, page_size: int = 50
    ) -> dict:
        """互动提醒列表（msgfeed 聚合），用于验证 @ 通知是否触达被 @ 用户。"""
        return await self._req(
            lambda: self._get(
                "/api/v1/message/event/list",
                mid,
                {"event_type": event_type, "page_size": page_size},
            ),
            f"event list mid={mid} type={event_type}",
        )

    # ==================== 收藏夹体系 ====================

    async def create_folder(self, mid: int, name: str, cover_url: str | None) -> str:
        """创建收藏夹并返回 folderId（封面先审后发：落 pending 审核）。"""
        body: dict = {"name": name, "description": f"{name}（seed 生成）"}
        if cover_url:
            body["coverUrl"] = cover_url
        try:
            data = await self._req(
                lambda: self._post("/api/v1/favorite/folder/create", mid, body),
                f"folder create mid={mid}",
            )
        except RuntimeError as e:
            # 封面 URL 校验失败（422）时降级为不带封面创建
            logger.warning(f"收藏夹封面校验失败，降级为无封面创建: {e}")
            data = await self._req(
                lambda: self._post(
                    "/api/v1/favorite/folder/create",
                    mid,
                    {"name": name, "description": f"{name}（seed 生成）"},
                ),
                f"folder create no-cover mid={mid}",
            )
        return data.get("folderId") or ""

    async def approve_folder_cover(self, folder_id: str) -> None:
        """管理员审核收藏夹封面通过（root，从 pending 队列取 pk）。"""
        try:
            data = await self._get(
                "/api/v1/favorite/folder/cover/audit/list",
                self.admin_mid,
                {"page_num": 1, "page_size": 20},
                role="root",
            )
        except Exception:
            data = {}
        items = data.get("items") or []
        target = next(
            (it for it in items if str(it.get("folderId")) == str(folder_id)), None
        )
        if not target:
            logger.warning(f"收藏夹 {folder_id} 无 pending 封面审核记录，跳过审核。")
            return
        await self._req(
            lambda: self._post(
                "/api/v1/favorite/folder/cover/audit/approve",
                self.admin_mid,
                {"pk": target["pk"], "remark": "SEED approve"},
                role="root",
            ),
            f"folder cover approve folder={folder_id}",
        )

    async def favorite_add(self, mid: int, dyn_id: int, folder_id: str) -> None:
        await self._req(
            lambda: self._post(
                "/api/v1/favorite/add",
                mid,
                {"bizType": InteractionBizTypeEnum.DYNAMIC, "bizId": str(dyn_id), "folderId": folder_id},
            ),
            f"favorite add mid={mid} dyn={dyn_id}",
        )

    async def favorite_setting(self, mid: int, show: bool) -> None:
        await self._req(
            lambda: self._post(
                "/api/v1/favorite/setting", mid, {"showFavorites": show}
            ),
            f"favorite setting mid={mid}",
        )

    # ==================== 关注 / 拉黑 ====================

    async def follow(self, mid: int, target_mid: int) -> None:
        await self._req(
            lambda: self._post(
                "/api/v1/message/follow/do", mid, {"target_mid": target_mid}
            ),
            f"follow {mid}->{target_mid}",
        )

    async def block(self, mid: int, target_mid: int) -> None:
        await self._req(
            lambda: self._post(
                "/api/v1/message/follow/block", mid, {"target_mid": target_mid}
            ),
            f"block {mid}->{target_mid}",
        )

    async def unblock(self, mid: int, target_mid: int) -> None:
        """解除拉黑（用于把历史累积的黑名单收敛到每人 1 条，走业务接口不直写库）。"""
        await self._req(
            lambda: self._post(
                "/api/v1/message/follow/unblock", mid, {"target_mid": target_mid}
            ),
            f"unblock {mid}->{target_mid}",
        )

    # ==================== 事件通知 / 系统通知 ====================

    async def report_event(
        self,
        receiver_mid: int,
        event_type: InteractionActionTypeEnum,
        dyn_id: int,
        actor_mid: int,
        actor_name: str | None,
        biz_id: str,
        biz_type: InteractionBizTypeEnum = InteractionBizTypeEnum.DYNAMIC,
    ) -> None:
        """上报互动事件（like / reply / at），接口不要求登录态，mid 在 body。

        ``biz_type`` 默认 DYNAMIC；lottery 等通用资源点赞后端不自动生成事件，
        由调用方显式上报并传入 ``InteractionBizTypeEnum.LOTTERY``。
        """
        try:
            await self._req(
                lambda: self._post(
                    "/api/v1/message/event/report",
                    receiver_mid,
                    {
                        "mid": receiver_mid,
                        "event_type": event_type,
                        "source_type": biz_type,
                        "source_id": str(dyn_id),
                        "actor_mid": actor_mid,
                        "content": _rr.pick(_COMMENTS),
                        "biz_id": biz_id,
                    },
                ),
                f"event report {event_type} -> {receiver_mid}",
            )
        except RuntimeError as e:
            logger.warning(f"事件上报失败（消息设置闸门/幂等）: {e}")

    async def notify_admin_create(self, title: str, content: str) -> int:
        """系统通知发布（root）。返回通知 id。"""
        data = await self._req(
            lambda: self._post(
                "/api/v1/message/notify/admin/create",
                self.admin_mid,
                {
                    "title": title,
                    "content": content,
                    "target_type": NotifyTargetTypeEnum.ALL,
                    "publish_now": True,
                },
                role="root",
            ),
            f"notify create {title}",
        )
        return int(data.get("id") or 0)

    # ==================== 私信 ====================

    async def dm_send(
        self, mid: int, receiver_mid: int, receiver_name: str | None
    ) -> str:
        """发送私信并返回 msgkey。"""
        data = await self._req(
            lambda: self._post(
                "/api/v1/message/dm/send",
                mid,
                {
                    "receiver_mid": receiver_mid,
                    "content": _rr.pick(_COMMENTS),
                    "receiver_name": receiver_name,
                },
            ),
            f"dm send {mid}->{receiver_mid}",
        )
        return data.get("msgkey") or ""

    async def approve_dm(self, msgkey: str) -> None:
        """私信审核通过（root，op=pass）。"""
        if not msgkey:
            return
        await self._req(
            lambda: self._post(
                "/api/v1/message/dm/admin/audit",
                self.admin_mid,
                {"msgkey": msgkey, "op": "pass"},
                role="root",
            ),
            f"dm approve msgkey={msgkey}",
        )

    async def dm_ack(self, mid: int, talker_mid: int) -> None:
        await self._req(
            lambda: self._post(
                "/api/v1/message/dm/ack", mid, {"talker_mid": talker_mid}
            ),
            f"dm ack {mid}<->{talker_mid}",
        )

    async def dm_messages(self, mid: int, talker_mid: int) -> dict:
        """拉取聊天记录（/messages），用于验证发送可见 / 撤回记录 / 删除后可见性。"""
        return await self._req(
            lambda: self._get(
                "/api/v1/message/dm/messages",
                mid,
                {"talker_mid": talker_mid, "page_size": 50},
            ),
            f"dm messages {mid}<->{talker_mid}",
        )

    async def dm_recall(self, mid: int, msgkey: str) -> tuple[bool, str]:
        """撤回私信，返回 (是否成功, 服务端消息)。失败（400 业务码）不抛异常。"""
        try:
            data = await self._req(
                lambda: self._post(
                    "/api/v1/message/dm/recall", mid, {"msgkey": msgkey}
                ),
                f"dm recall mid={mid} msgkey={msgkey}",
            )
            return True, data.get("message") or "撤回成功"
        except RuntimeError as e:
            return False, str(e)

    async def dm_delete(self, mid: int, msgkeys: list[str]) -> dict:
        """删除私信（仅自己视角，对方仍可见）。"""
        return await self._req(
            lambda: self._post("/api/v1/message/dm/delete", mid, {"msgkeys": msgkeys}),
            f"dm delete mid={mid}",
        )

    async def feed_following(self, mid: int) -> dict:
        """关注流：返回关注作者发布的动态。"""
        return await self._req(
            lambda: self._get(
                "/api/v1/community/feed/following", mid, {"page": 1, "page_size": 50}
            ),
            f"feed following mid={mid}",
        )

    # ==================== 用户举报 / 封禁 ====================

    async def report_user(self, mid: int, target_mid: int) -> None:
        """用户空间举报（统一举报 bizType=user）。"""
        await self._req(
            lambda: self._post(
                "/api/v1/report",
                mid,
                {
                    "bizType": InteractionBizTypeEnum.USER,
                    "bizId": target_mid,
                    "reasonType": _REPORT_REASON_TYPE,
                    "reasonDesc": "seed 举报用户空间",
                },
            ),
            f"report user {mid}->{target_mid}",
        )

    async def ban_user(self, target_mid: int) -> None:
        """封禁用户（root，comment 服务，临时 7 天）。"""
        await self._req(
            lambda: self._post(
                "/api/v1/message/admin/ban",
                self.admin_mid,
                {
                    "mids": [target_mid],
                    "ban_services": _BAN_SERVICES,
                    "reason": "seed 封禁演示",
                    "duration_type": BanDurationTypeEnum.TEMPORARY,
                    "duration_days": 7,
                },
                role="root",
            ),
            f"ban user {target_mid}",
        )

    # ==================== 头像审核流 ====================

    async def submit_avatar(self, mid: int, avatar_url: str) -> str:
        """提交头像更换（→ TUserAvatarAudit pending；返回 avatar_status）。"""
        try:
            data = await self._req(
                lambda: self._post(
                    "/api/v1/user/user_info/update",
                    mid,
                    {"avatar": avatar_url},
                ),
                f"avatar submit mid={mid}",
            )
            return data.get("avatar_status") or ""
        except RuntimeError as e:
            logger.warning(f"头像提交失败（下载校验/网络）: {e}")
            return ""

    async def approve_avatar(self) -> None:
        """管理员审核头像通过（root，从 pending 队列取 pk）。"""
        try:
            data = await self._get(
                "/api/v1/user/avatar/audit/list",
                self.admin_mid,
                {"page_num": 1, "page_size": 20},
                role="root",
            )
        except RuntimeError:
            data = {}
        items = data.get("items") or []
        if not items:
            logger.warning("头像审核队列为空，跳过头像审核。")
            return
        for it in items[:3]:
            try:
                await self._req(
                    lambda: self._post(
                        "/api/v1/user/avatar/audit/approve",
                        self.admin_mid,
                        {"pk": it["pk"], "remark": "SEED approve"},
                        role="root",
                    ),
                    f"avatar approve pk={it['pk']}",
                )
            except RuntimeError as e:
                logger.warning(f"头像审核失败 pk={it['pk']}: {e}")
