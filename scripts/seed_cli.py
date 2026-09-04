"""统一 seed 脚本（单文件，纯 HTTP 接口调用版）。

合并原 ``seed_via_api.py``（全互动联调）与 ``seed_moment_bulk.py``（大数据灌数）为单一入口，
**一个命令跑完全部**，默认参数即可用、无需额外配置：

    uv run python scripts/seed_cli.py                  # 全互动联调 + 大数据灌数
    uv run python scripts/seed_cli.py --skip-bulk       # 仅全互动联调
    uv run python scripts/seed_cli.py --skip-full       # 仅大数据灌数
    uv run python scripts/seed_cli.py --dry-run         # 只打印计划

**全互动体系（4 大类 18 项，纯接口调用、断言失败响亮报错）**
① 动态体系：话题创建/审核、发布/审核、点赞、转发、浏览、举报、置顶
② 评论体系：一级评论/审核、楼中楼、评论点赞/点踩、@提及、评论举报、置顶
③ 用户级互动：收藏夹创建/封面审核、收藏、收藏设置、关注、拉黑、事件通知、系统通知
④ 消息与管理：私信发送/审核/撤回/删除全流程、通用计数、用户举报、封禁、头像审核流

**大数据灌数（基于 biliopusdb 真实动态 + crawler 真实 lottery + pptr 用户池私信，走 API，软降级跳过）**
从 biliopusdb 拉取真实动态/话题，从 crawler 的 GetAllLottery 接口（不可达时降级直连 dyndetail.lotdata）
取真实 lottery_id，从 pptr Postgres 取真实用户池，经 be-message HTTP 接口批量灌入动态 +
点赞/评论/@/浏览 + lottery 评论套件 + 私信，用于性能/联调。
每个资源（动态 / lottery）走统一评论套件 ``_seed_bulk_comment_suite``：一级评论（正文末尾随机 @）+
楼中楼（二级评论）+ 评论点赞/点踩 + 显式 @ + 举报 + 置顶；lottery 资源额外补发 LIKE 事件
（通用资源点赞后端不自动生成）。私信走 ``_seed_bulk_dm``：在 pptr 用户池上 O(n²) 批量互发，
覆盖动态 / 评论 / @ 外，也覆盖「用户对网络」广度，使登录 demo 的任意 pptr 用户（如测试账号 127763472）
都有私信内容可读。评论/私信均走真实链路，触发 REPLY/AT 事件通知与站内信，使消息中心拥有可联调的真实数据。

**动态 + lottery 混合资源池（全互动联调阶段）**
调 crawler 的 GetAllLottery HTTP 接口取真实 lottery_id（接口不可达时降级直连
dyndetail.lotdata），与已发布动态**交错**组成统一资源池 ``[(oid, biz_type)]``
（``_build_resource_pool``，交错而非拼接，避免「前半段全动态、后半段全抽奖」的覆盖偏斜），
评论体系在这个混合池上跑：评论 / 楼中楼 / @ 走评论子系统自动生成 REPLY/AT 事件，
资源点赞按 biz_type 分流（DYNAMIC→thumb、LOTTERY→thumb_lottery），置顶与列表回查同步透传
biz_type。lottery 独有的缺口是**通用资源点赞后端不自动生成 LIKE 事件**（动态点赞会生成），
由 ``seed_lottery_resource`` 显式补发，事件统一落点同一测试用户，便于登录消息中心查看。

**@ 提及覆盖（联调 + 灌数两阶段）**
动态正文与评论正文末尾统一追加**随机 @**：动态用 `AT` 富文本节点（`bizId`=被@ mid、
`name`=昵称），评论用 `@昵称` 文本 + `at_name_to_mid` 映射（服务端归一为 `@{mid}` 占位符）。
追加随机 @ 同时兼作正文去重，规避「同用户同正文 10s 内 >3 次」的评论限流（Phase 2.6）。
两阶段落库后回查动态详情 / 评论列表断言 @ 已生效（AT 节点回显、`@{mid}` 渲染回 `@昵称`），
并再查被 @ 用户的 AT 事件提醒，验证 @ 通知链路触达。

说明：
- 全部走 be-message-service 真实 HTTP 业务链路（``x-bili-*`` 头模拟网关身份；root 审核统一走 ``--admin-mid``），
  不直接写 MySQL 主库；仅只读回查 pptr Postgres / be-message / biliopusdb 确认前置数据存在；
- 作者/互动者取自 pptr Postgres 真实用户（只读）；大数据灌数作者/点赞者/浏览者统一映射自有用户系统；
- 全互动阶段断言失败会**响亮报错**（暴露代码 bug）；大数据灌数阶段（biliopusdb/连接问题）**软降级跳过**，
  不阻断前面已完成的全互动阶段；
- 私信覆盖 a→b 与 b→a **双向互发**：每个方向均验证「发送 → 拉记录可见 → 撤回
  （验证双方 RECALLED + recalled_by 落库与出参）→ 再发送 → 单方面删除
  （验证自己视角不可见、对方仍可见）→ 删除后尝试撤回被拒」全流程；
  另对**多对用户**双向互发（发送 → 审核 → 已读 → 双方可见），覆盖会话网络广度；
- **黑名单归一 + 私信避让**：黑名单持久化且跨 seed 累积，每多一条就多一个用户被
  静默排除在评论 / 私信 / 关注 / @ 通知之外。故 **每人只保留 1 条黑名单**
  （够验证「拉黑后不可互动」即可），其余由 ``_normalize_blocklist`` 走
  ``POST /message/follow/unblock`` 解除；归一后的关系集合透传给私信场景，
  配对按「接收方是否已拉黑发送方」单向判定主动避让（对齐
  ``FollowService.is_blocked_by(sender, receiver)``），保证撤回 / 删除这类
  **响亮断言**不会因历史黑名单整段失败。
"""
import argparse
import asyncio
import os
import random
import re
import sys
import uuid
from collections.abc import Sequence
from itertools import cycle, zip_longest
from pathlib import Path
from typing import TypeVar
# 允许 scripts/ 目录下直接运行：注入项目根目录到 sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import aiomysql
import httpx
from loguru import logger
from sqlalchemy import func
from sqlalchemy.engine import make_url
from sqlmodel import col, select
from tqdm import tqdm

from app.core.config import settings
from app.core.database import new_pptr_session, new_session
from app.models.db.dm_tbl import DmMessageIndex, DmSession
from app.models.db.follow_tbl import UserFollow
from app.models.db.setting_tbl import UserMessageSetting
from bili_common.models import InteractionActionTypeEnum, InteractionBizTypeEnum
from app.models.enums import (
    DmMsgStatusEnum,
    DmRelationEnum,
    FollowStatusEnum,
    NotifyTargetTypeEnum,
    BanDurationTypeEnum,
)
from app.models.pptr_db import PptrUserDetail, PptrUserInfo

# ---------------------------------------------------------------------------
# 超时配置（可用环境变量覆盖，默认值已覆盖雪花 ID 分钟级等待）
# ---------------------------------------------------------------------------
# 背景：对外短雪花 ID（moment_id 等）位布局为「分钟时间戳 31bit + worker 4bit
# + 序列号 4bit」，每个 worker 每分钟最多生成 16 个。灌数速率超过该上限时，
# 服务端会在锁外 sleep 到下一分钟（最长 60s）。客户端超时若 < 60s，会在分钟
# 边界误报 ReadTimeout。因此：
# - SEED_HTTP_TIMEOUT：httpx 单次传输超时，默认 90s（= 跨分钟等待 + 处理余量）；
# - SEED_REQ_TIMEOUT：_req 单次请求总超时（asyncio.wait_for），默认 120s；
# - SEED_REQ_CONCURRENCY：客户端同时在途 HTTP 请求上限。必须压在 be-message 的
#   MySQL 连接池（pool_size + max_overflow，运行时常见 20+30=50）以内，否则服务端
#   会出现 QueuePool 耗尽 → TimeoutError → 500（见 logs/message-service.log）。
#   注意：单个动态任务内部还会 fan-out 出大量点赞/评论/浏览子请求，每个都是一次
#   独立 HTTP（独立占用一个服务端 DB 连接），所以仅限制「任务数」(sem) 不够，
#   必须在「每次请求」层面加全局信号量。弱事件上报（@ 通知）也会额外占用连接，
#   故留出余量，默认取 20（≤ 服务端 pool_size）。
_SEED_HTTP_TIMEOUT = float(os.environ.get("SEED_HTTP_TIMEOUT", "90"))
_SEED_REQ_TIMEOUT = float(os.environ.get("SEED_REQ_TIMEOUT", "120"))
_SEED_REQ_CONCURRENCY = int(os.environ.get("SEED_REQ_CONCURRENCY", "20"))


# ---------------------------------------------------------------------------
# 真实感素材池（全互动联调用）
# ---------------------------------------------------------------------------

_SENTENCES = [
    "今天天气真好，出门溜达了一圈，随手记录一下～",
    "刚看完一个新番，剧情太顶了，强烈安利（划掉）安利给各位！",
    "深夜放毒：自己在家做了一顿火锅，食材新鲜到爆炸。",
    "学习了一下新的前端框架，组件化思路真的香。",
    "周末和朋友去了趟展子，人好多但氛围拉满。",
    "分享一个小技巧：写代码前先画流程图，效率直接翻倍。",
    "今天又被 bug 折磨了一整天，但最后修好的瞬间真爽。",
    "新到的机械键盘手感绝了，码字都变快乐了。",
    "养的多肉终于开花了，养了快两年没白费。",
    "楼下新开的奶茶店，第二杯半价，冲就完事了。",
    "健身第 30 天，腹肌隐约有点轮廓了哈哈。",
    "重读了《活着》，每次看都有新的触动。",
    "把房间重新布置了一遍，现在敲代码心情都好了。",
    "试着做了一期 vlog，剪辑比想象中费时间。",
    "今天跑步五公里，配速终于破六了！",
    "刚入手的相机拍了几张夜景，直出就很能打。",
    "周末打算去爬山，有一起的吗？评论区扣 1。",
    "把收藏夹里吃灰的教程都看了，知识焦虑缓解一点。",
    "新游戏首周体验，肝到凌晨但不亏。",
    "最近在练书法，字还是丑，但静心效果一流。",
]

_COMMENTS = [
    "沙发！说的太好了",
    "同感同感，握个手",
    "学到了，感谢分享！",
    "哈哈哈哈笑死我了",
    "这也太真实了吧",
    "蹲一个后续",
    "已三连，期待更新～",
    "楼主说得有道理",
    "前排围观大佬",
    "这种内容多来点",
    "收藏了，慢慢看",
    "有一说一，确实",
    "这就是我想要的",
    "不明觉厉",
    "顶上去让更多人看到",
    "太强了，学到了学到了",
    "路过支持一下",
    "nice，这个可以有",
    "看完心情都变好了",
    "什么时候更新下一期",
]

# 楼中楼回复语
_REPLIES = [
    "回复层主：完全同意",
    "补充一点，楼上说得对",
    "这楼我先占住了",
    "楼上说的有道理但不够全面",
    "借楼问一下，这个怎么操作？",
    "回复 1 楼：别闹哈哈",
]

_IMG_URLS = [
    # 带图片后缀的真实图源（头像 / 封面校验要求路径以 .jpg/.png/.webp 等结尾）
    "https://picsum.photos/seed/bili1/600/400.jpg",
    "https://picsum.photos/seed/bili2/600/400.jpg",
    "https://picsum.photos/seed/bili3/600/400.png",
    "https://picsum.photos/seed/bili4/600/400.webp",
]

_TOPIC_NAMES = ["日常", "美食", "动漫", "编程", "健身", "旅行", "摄影", "读书"]

#: 素材池是否已从外库真实数据加载（幂等，启动时加载一次）
_MATERIAL_LOADED = True

# 举报原因：与 ReportReasonEnum / MomentReportReasonEnum 对齐（1-6）
_REPORT_REASON_TYPE = 3  # 人身攻击
_BAN_SERVICES = ["comment"]

#: 混合资源池容量：评论/at/点赞在「动态 + lottery」混合池上进行，
#: 动态取前 10 个（原行为），lottery 取 2 个（通用资源链路覆盖够用即可，
#: 多了会挤占动态的评论额度且放大 crawler RPC 校验耗时）
_DYN_POOL_SIZE = 10
_LOTTERY_POOL_SIZE = 2


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------


def _headers(mid: int, *, role: str = "normal") -> dict[str, str]:
    """构造模拟 be-gateway 注入的请求头（x-bili-*）。"""
    return {
        "x-bili-mid": str(mid),
        "x-bili-role": role,
        "x-bili-user-name": f"user{mid}",
    }


def _at_targets_deterministic(
    users: list[tuple[int, str | None]],
    exclude_mid: int | None,
    max_n: int = 1,
) -> list[tuple[int, str]]:
    """确定性轮遍 @ 目标：从 ``users`` 里（排除 ``exclude_mid``）轮流取 ``max_n`` 个。

    取代 ``_random_at_targets`` 的随机挑：用户/素材定值轮遍、不重复。
    """
    pool = [u for u in users if u[0] != exclude_mid]
    out: list[tuple[int, str]] = []
    for _ in range(max_n):
        if not pool:
            break
        mid, name = _rr.pick(pool)
        out.append((int(mid), (name or f"user{mid}")))
    return out


def _at_text_suffix(targets: list[tuple[int, str]]) -> str:
    """评论正文末尾追加的 @ 文本：`` @昵称 @昵称2``（无目标时为空串）。"""
    return "".join(f" @{name}" for _, name in targets)


def _at_name_to_mid(targets: list[tuple[int, str]]) -> dict[str, int]:
    """@ 昵称 → mid 映射（服务端据此把正文里的 `@昵称` 归一为 `@{mid}` 占位符）。"""
    return {name: mid for mid, name in targets}


def _at_nodes(targets: list[tuple[int, str]]) -> list[dict]:
    """动态正文末尾追加的 @ 富文本节点（AT 节点：`bizId`=被@ mid，`name`=昵称）。"""
    nodes: list[dict] = []
    for mid, name in targets:
        nodes.append({"type": "WORDS", "text": " "})
        nodes.append({"type": "AT", "bizId": str(mid), "name": name})
    return nodes


def _content_nodes(
    sentence: str,
    at_targets: list[tuple[int, str]] | None = None,
    *,
    with_image: bool = False,
) -> list[dict]:
    """构造富文本节点（WORDS + 末尾 @ 节点 + 可选外链图片 LINK）。

    ``with_image``：是否附带一张轮遍选取的外链图片（由调用方用确定性轮遍决定，
    不再在函数内部随机 30%）。
    """
    nodes: list[dict] = [{"type": "WORDS", "text": sentence}]
    nodes.extend(_at_nodes(at_targets or []))
    if with_image:
        url = _rr.pick(_IMG_URLS)
        nodes.append({"type": "WORDS", "text": " "})
        nodes.append(
            {
                "type": "LINK",
                "text": url,
                "jumpUrl": url,
                "picMeta": {"renderAsImage": True},
            }
        )
    return nodes


async def _fetch_real_users(limit: int) -> list[tuple[int, str | None]]:
    """从 pptr Postgres 随机取若干真实用户 (uid, uname) —— 只读，不写 pptr。"""
    async with new_pptr_session() as s:
        stmt = (
            select(PptrUserInfo.uid, PptrUserDetail.uname)
            .join(
                PptrUserDetail,
                col(PptrUserDetail.mid) == col(PptrUserInfo.uid),
                isouter=True,
            )
            .where(col(PptrUserInfo.deletedAt).is_(None))
            .where(col(PptrUserDetail.uname).isnot(None))
            .order_by(func.random())
            .limit(limit)
        )
        rows = (await s.exec(stmt)).all()
    users = [(int(uid), uname) for uid, uname in rows]
    if not users:
        logger.warning("pptr 库未取到任何真实用户，请确认 pptr 数据库连接与数据。")
    return users


# ---------------------------------------------------------------------------
# be-message MySQL 只读探针：每个动作前查询数据库确认前置数据存在，模拟真实用户操作
# ---------------------------------------------------------------------------


async def _accept_stranger_dm(receiver_mid: int) -> bool:
    """查询接收方是否接受陌生人私信（msg_user_setting；无记录默认 True）。"""
    async with new_session() as s:
        row = (
            await s.exec(
                select(UserMessageSetting).where(UserMessageSetting.mid == receiver_mid)
            )
        ).one_or_none()
    return bool(row.recv_stranger_dm) if row else True


async def _session_relation(owner_mid: int, talker_mid: int) -> DmRelationEnum | None:
    """查询 owner 视角会话关系（msg_dm_session；无会话返回 None）。"""
    async with new_session() as s:
        row = (
            await s.exec(
                select(DmSession).where(
                    DmSession.owner_mid == owner_mid,
                    DmSession.talker_mid == talker_mid,
                )
            )
        ).one_or_none()
    return row.relation if row else None


async def _dm_index_by_msgkey(msgkey: int) -> list[DmMessageIndex]:
    """查询某条私信的双方索引行（msg_dm_index），确认写扩散落库。"""
    async with new_session() as s:
        rows = (
            await s.exec(select(DmMessageIndex).where(DmMessageIndex.msgkey == msgkey))
        ).all()
    return list(rows)


async def _pick_dm_pair(
    users: list[tuple[int, str | None]],
    blocked: set[tuple[int, int]] | None = None,
) -> tuple[tuple[int, str | None], tuple[int, str | None]]:
    """选一对可正常收发私信的用户：接收方须接受陌生人私信（查 msg_user_setting）。

    同时按 ``blocked`` 双向避让——a→b 与 b→a 任一方向会被黑名单拒绝就换下一对，
    避免深度撤回 / 删除场景（断言响亮报错）因历史黑名单而整段失败。
    先取前若干对尝试，避免整个 seed 因发送被 filtered 而失败。
    """
    blocked = blocked or set()
    for i in range(min(5, len(users) - 1)):
        a, b = users[i], users[i + 1]
        if _is_dm_blocked(blocked, a[0], b[0]) or _is_dm_blocked(blocked, b[0], a[0]):
            logger.warning(f"用户对 {a[0]}<->{b[0]} 存在黑名单关系，尝试下一对…")
            continue
        if await _accept_stranger_dm(b[0]):
            return a, b
        logger.warning(f"用户 {b[0]} 关闭了陌生人私信，尝试下一对…")
    # 兜底：直接用前两个用户（即使可能被过滤，发送方视角仍会写入）
    return users[0], users[1]


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
            except (asyncio.TimeoutError, httpx.TransportError) as e:
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

    async def browse(self, mid: int, dyn_id: int) -> None:
        """详情浏览触发（detail 接口兼作浏览统计触发点，投递浏览 MQ）。"""
        await self._req(
            lambda: self._get(
                f"/api/v1/community/interaction/status/{dyn_id}",
                mid,
                {"bizType": InteractionBizTypeEnum.DYNAMIC},
            ),
            f"browse mid={mid} dyn={dyn_id}",
        )

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


# ---------------------------------------------------------------------------
# @ 提及验证（动态 / 评论两处落库后回查）
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# 四大模块编排（全互动联调）
# ---------------------------------------------------------------------------


async def seed_moment(
    client: SeedClient,
    users: list[tuple[int, str | None]],
    count: int,
    concurrency: int,
) -> list[int]:
    """① 动态体系：话题 + 发布/审核 + 点赞 + 转发 + 浏览 + 举报 + 置顶。

    动态创建按 ``concurrency`` 并发执行（``asyncio.Semaphore`` 限流），
    单条失败软降级跳过，不影响整体进度；转发池 ``normal_ids`` 等共享状态
    在单事件循环内由主协程聚合，无竞态。
    """
    # 作者在全部用户上轮流循环（round-robin），保证每个用户都被轮到创建动态，
    # 总次数 = count（不再只用前半用户 / 随机选）。
    author_cycle = cycle(users)
    normal_ids: list[int] = []
    forward_ids: list[int] = []
    like_count = 0
    topic_ids: list[int] = []

    # 建话题 + 审核通过（话题名全局唯一；话题名轮遍 _TOPIC_NAMES，全局唯一用递增序号后缀，
    # 不再用 uuid4 随机后缀——序号由 _rr 游标保证不重复）。
    creator_mid, _ = next(author_cycle)
    topic_seq = 0
    for _ in range(2):
        topic_seq += 1
        name = f"{_rr.pick(_TOPIC_NAMES)}_{topic_seq}"
        try:
            tid = await client.create_topic(creator_mid, name)
            await client.approve_topic(tid)
            topic_ids.append(tid)
        except RuntimeError as e:
            logger.warning(f"话题创建/审核失败: {e}")
    logger.info(f"已建并过审话题 {len(topic_ids)} 个")

    sem = asyncio.Semaphore(concurrency)

    def _rr_at_targets(exclude_mid: int | None) -> list[tuple[int, str]]:
        """确定性轮遍 @ 目标：从 users 里（排除自己）轮流取 1 个。"""
        pool = [u for u in users if u[0] != exclude_mid]
        if not pool:
            return []
        mid, name = _rr.pick(pool)
        return [(int(mid), (name or f"user{mid}"))]

    async def _one() -> tuple[int | None, int | None, int | None, int]:
        """创建一条动态并完成 发布/审核/点赞/浏览/举报/转发（全定值轮遍）。"""
        async with sem:
            author_mid, _ = next(author_cycle)
            sentence = _rr.pick(_SENTENCES)
            # 正文末尾追加 @（不 @ 自己），覆盖动态 @ 落库 + 事件通知链路
            at_targets = _rr_at_targets(author_mid)
            # 话题：轮流挂载（topic_ids 非空时每 2 条带 1 个话题，避免全部/全不带）
            topic_id = _rr.pick(topic_ids) if (topic_ids and _rr.pick(range(2)) == 0) else None

            # 1) 发布（正文图轮遍 _IMG_URLS，非随机 30%）
            content = _content_nodes(sentence, at_targets, with_image=_rr.pick(range(10)) == 0)
            dyn_id = await client.create_dynamic(
                author_mid,
                scene="WORD",
                content=content,
                topic_id=topic_id,
            )
            # 2) 审核通过 → normal
            await client.approve(dyn_id)

            # 3) 点赞：除作者外轮流取 3 个点赞（用户不足则全点）
            likers = [u for u in users if u[0] != author_mid]
            n_like = 0
            for liker in _rr.pick_n(likers, min(3, len(likers))):
                await client.thumb(liker[0], dyn_id)
                n_like += 1

            # 4) 浏览（detail 触发浏览 MQ）：轮遍取 1 个非作者
            if likers:
                viewer = _rr.pick(likers)[0]
                await client.browse(viewer, dyn_id)

            # 5) 举报：每 10 条动态举报 1 次（确定性，不再概率随机）
            if likers and _rr.pick(range(10)) == 0:
                await client.report_moment(_rr.pick(likers)[0], dyn_id)

            # 6) 转发：对创建好的动态尝试转发——每创建一条后，若已有过审动态，
            #    就从已过审集合里轮遍取一条作为源进行转发（覆盖转发链路），不再概率触发。
            fwd_id: int | None = None
            if normal_ids:
                src_dyn = _rr.pick(normal_ids)
                fwd_id = await client.repost(
                    author_mid,
                    src_dyn,
                    _content_nodes(
                        "转发：这个说得太对了",
                        _rr_at_targets(author_mid),
                        with_image=False,
                    ),
                )
                await client.approve(fwd_id)

            return author_mid, dyn_id, fwd_id, n_like

    # 并发创建（asyncio.Semaphore 限流），tqdm 实时进度
    tasks = [asyncio.create_task(_one()) for _ in range(count)]
    first_dyn_owner: int | None = None
    for f in tqdm(
        asyncio.as_completed(tasks), total=count, desc="[moment] 动态", unit="条"
    ):
        try:
            author_mid, dyn_id, fwd_id, n_like = await f
            normal_ids.append(dyn_id)
            if fwd_id is not None:
                forward_ids.append(fwd_id)
            like_count += n_like
            if first_dyn_owner is None:
                first_dyn_owner = author_mid
        except RuntimeError as e:
            logger.warning(f"单条动态任务异常（已跳过）: {e}")

    # 7) 置顶第一条动态（作者本人 + normal）
    if normal_ids and first_dyn_owner is not None:
        try:
            await client.top_moment(first_dyn_owner, normal_ids[0])
        except RuntimeError as e:
            logger.warning(f"置顶失败（动态非本人/non-normal）: {e}")

    # 8) @ 验证：回查详情，确认正文末尾随机 @ 已落库 + 渲染 + 触发 AT 通知
    if normal_ids:
        await _verify_dynamic_at(client, users[0][0], normal_ids[0])

    logger.success(
        f"[动态体系] 动态 {len(normal_ids)} 条（含转发 {len(forward_ids)}），点赞 {like_count} 次，话题 {len(topic_ids)} 个"
    )
    return normal_ids


async def seed_comment(
    client: SeedClient,
    users: list[tuple[int, str | None]],
    resources: list[tuple[int, InteractionBizTypeEnum]],
) -> None:
    """② 评论体系：一级评论 + 楼中楼 + 点赞/点踩 + @（每条正文末尾随机 @）+ 举报 + 置顶。

    ``resources`` 是**动态与 lottery 混合**的资源池 ``[(oid, biz_type)]``：
    同一套互动在两种 biz_type 上各跑一遍，保证通用资源链路与动态链路覆盖对等，
    不会出现「评论只覆盖动态、点赞只覆盖抽奖」的偏斜。
    """
    if not resources:
        logger.warning("无可用资源（动态与 lottery 均为空），跳过评论体系。")
        return

    root_rpids: list[str] = []
    comment_count = 0
    action_count = 0
    like_count = 0

    for oid, biz_type in tqdm(resources, desc="[评论体系] 资源", unit="个"):
        # 评论区 up_mid 用资源作者（动态从用户池轮遍取）
        up_mid = _rr.pick(users)[0]
        commenters = [u for u in users if u[0] != up_mid]
        # 仅当前资源的根评论（楼中楼/点赞/置顶必须限定在同一个评论区）
        dyn_root_rpids: list[str] = []

        def _rr_at_targets(exclude_mid: int | None) -> list[tuple[int, str]]:
            """确定性轮遍 @ 目标：从 commenters（排除自己）轮流取 1 个。"""
            pool = [u for u in commenters if u[0] != exclude_mid]
            if not pool:
                return []
            mid, name = _rr.pick(pool)
            return [(int(mid), (name or f"user{mid}"))]

        # 0) 资源点赞：动态走 thumb，lottery 等通用资源走 thumb_lottery
        #    （通用资源点赞后端不自动生成 LIKE 事件，消息中心覆盖由
        #     seed_lottery_resource 显式补发，此处不重复补）
        for u in _rr.pick_n(commenters, min(2, len(commenters))):
            if biz_type is InteractionBizTypeEnum.LOTTERY:
                await client.thumb_lottery(u[0], oid)
            else:
                await client.thumb(u[0], oid)
            like_count += 1

        # 1) 一级评论（固定 2 条，轮遍取评论者）+ 审核通过（正文末尾轮遍 @，不 @ 自己）
        for commenter in _rr.pick_n(commenters, min(2, len(commenters))):
            try:
                rpid = await client.add_comment(
                    commenter[0],
                    oid,
                    up_mid,
                    biz_type=biz_type,
                    at_users=_rr_at_targets(commenter[0]),
                )
                await client.approve_comment(rpid)
            except RuntimeError as e:
                # 拉黑等业务限制会拒绝评论（seed 组合可能命中持久化黑名单），软降级跳过
                logger.warning(f"评论发布失败（已跳过）: {e}")
                continue
            root_rpids.append(rpid)
            dyn_root_rpids.append(rpid)
            comment_count += 1
            await asyncio.sleep(0.02)

        # 2) 楼中楼：对当前资源每个根评论固定回复 1 层（轮遍取回复者）
        for root_rpid in dyn_root_rpids:
            parent = root_rpid
            replier = _rr.pick(commenters)
            try:
                rpid = await client.add_comment(
                    replier[0],
                    oid,
                    up_mid,
                    biz_type=biz_type,
                    root=root_rpid,
                    parent=parent,
                    message=_rr.pick(_REPLIES),
                    at_users=_rr_at_targets(replier[0]),
                )
                await client.approve_comment(rpid)
            except RuntimeError as e:
                # 同上：评论被业务拒绝（拉黑等）软降级跳过，不阻断楼中楼后续
                logger.warning(f"楼中楼评论发布失败（已跳过）: {e}")
                continue
            comment_count += 1
            parent = rpid
            await asyncio.sleep(0.02)

        # 3) 评论点赞 / 点踩（轮遍取动作者，赞/踩交替）
        for i, rpid in enumerate(dyn_root_rpids):
            actor = _rr.pick(commenters)
            await client.comment_action(actor[0], rpid, 1 if i % 2 == 0 else 2)
            action_count += 1

        # 4) @ 提及：一级评论带 at_mids + at_name_to_mid（显式 @ + 末尾轮遍 @ 并存）
        if len(commenters) >= 2:
            at_target = commenters[0]
            at_name = at_target[1] or f"user{at_target[0]}"
            subject = "抽奖" if biz_type is InteractionBizTypeEnum.LOTTERY else "动态"
            try:
                await client.add_comment(
                    commenters[1][0],
                    oid,
                    up_mid,
                    biz_type=biz_type,
                    message=f"@{at_name} 这个{subject}真不错",
                    at_mids=[at_target[0]],
                    at_name_to_mid={at_name: at_target[0]},
                    at_users=_rr_at_targets(commenters[1][0]),
                )
                comment_count += 1
            except RuntimeError as e:
                # 同上：@ 评论被业务拒绝（拉黑等）软降级跳过
                logger.warning(f"@ 评论发布失败（已跳过）: {e}")

        # 5) 举报当前资源的一条评论
        if dyn_root_rpids:
            await client.report_comment(_rr.pick(commenters)[0], dyn_root_rpids[-1])

        # 6) 评论置顶（资源作者身份，仅置顶当前资源的根评论）
        if dyn_root_rpids:
            try:
                await client.top_comment(
                    up_mid, oid, dyn_root_rpids[-1], biz_type=biz_type
                )
            except RuntimeError as e:
                logger.warning(f"评论置顶失败: {e}")

    # 7) @ 验证：每种 biz_type 各回查一个资源，确认随机 @ 已落库 + 渲染 + 触发 AT 通知
    verified: set[InteractionBizTypeEnum] = set()
    for oid, biz_type in resources:
        if biz_type in verified:
            continue
        verified.add(biz_type)
        await _verify_comment_at(client, users[0][0], oid, biz_type=biz_type)

    dyn_n = sum(1 for _, t in resources if t is InteractionBizTypeEnum.DYNAMIC)
    logger.success(
        f"[评论体系] 资源 {len(resources)} 个（动态 {dyn_n} / lottery "
        f"{len(resources) - dyn_n}）：评论 {comment_count} 条（含楼中楼），"
        f"资源点赞 {like_count} 次，评论互动 {action_count} 次，@/举报/置顶已覆盖"
    )


async def seed_interact(
    client: SeedClient,
    users: list[tuple[int, str | None]],
    normal_ids: list[int],
    *,
    skip_follow: bool = False,
) -> None:
    """③ 用户级互动：收藏夹 + 关注/拉黑 + 关注流验证 + 事件通知 + 系统通知。"""
    if not normal_ids:
        logger.warning("无可用动态，跳过用户级互动。")
        return

    folder_ids: list[str] = []
    # 1) 收藏夹：每人建一个（轮流带封面 → 封面审核）
    for i, (mid, _) in enumerate(users[: min(3, len(users))]):
        name = f"seed收藏夹{mid}"
        cover = _rr.pick(_IMG_URLS) if i % 2 == 0 else None
        fid = await client.create_folder(mid, name, cover)
        if fid:
            folder_ids.append(fid)
            await client.approve_folder_cover(fid)
    # 默认收藏夹（不传 folderId 走默认夹）
    if users:
        await client.favorite_setting(users[0][0], True)

    # 2) 收藏动态（每人收藏 1~2 条到自己的收藏夹）
    fav_count = 0
    for i, (mid, _) in tqdm(
        enumerate(users[: min(5, len(users))]), desc="[用户级互动] 收藏", unit="人"
    ):
        fid = folder_ids[i % len(folder_ids)] if folder_ids else None
        for dyn_id in _rr.pick_n(normal_ids, min(2, len(normal_ids))):
            try:
                if fid:
                    await client.favorite_add(mid, dyn_id, fid)
                else:
                    await client.favorite_add(mid, dyn_id, "")
                fav_count += 1
            except RuntimeError as e:
                logger.warning(f"收藏失败: {e}")

    # 3) 关注 / 拉黑
    hub = users[0]
    others = [u for u in users[1:] if u[0] != hub[0]]
    followed: list[int] = []
    for target in others[: min(3, len(others))]:
        try:
            await client.follow(hub[0], target[0])
            followed.append(target[0])
        except RuntimeError as e:
            # 业务拒绝（如对方已拉黑 → 400「无法关注」，黑名单持久化会跨 seed 命中）
            # 软降级跳过，不中断整体流程（与服务端行为一致：拉黑即互斥）
            logger.warning(f"关注失败（已跳过，可能被拉黑）: {e}")
    # 拉黑其中一个（演示黑名单场景）。**只拉黑这 1 个**——黑名单持久化且跨 seed
    # 累积，多拉一条就多一个用户被静默排除在评论 / 私信 / 关注之外（服务端静默
    # 拒绝，seed 只能软降级跳过）。历史累积的多余黑名单由后续
    # ``_normalize_blocklist`` 统一收敛到每人 1 条。
    if len(others) > 1:
        try:
            await client.block(hub[0], others[1][0])
        except RuntimeError as e:
            logger.warning(f"拉黑失败（已跳过）: {e}")
    # 3.1) 关注流验证（/feed/following 应返回关注作者的动态）
    if not skip_follow and followed:
        try:
            feed = await client.feed_following(hub[0])
            items = feed.get("items", [])
            feed_mids = {it["mid"] for it in items}
            logger.info(
                f"关注流验证：{hub[0]} 关注 {followed}，"
                f"/feed/following 返回 {len(items)} 条（作者 {sorted(feed_mids)[:10]}）"
            )
        except RuntimeError as e:
            logger.warning(f"关注流验证失败: {e}")

    # 4) 事件通知（like / reply / at 三类）
    target_mid = others[0][0] if others else hub[0]
    actor = hub
    for event_type in (InteractionActionTypeEnum.LIKE, InteractionActionTypeEnum.REPLY, InteractionActionTypeEnum.AT):
        await client.report_event(
            target_mid,
            event_type,
            _rr.pick(normal_ids),
            actor[0],
            actor[1],
            str(_rr.pick(normal_ids)),
        )

    # 5) 系统通知（root 发布，全体用户）
    await client.notify_admin_create(
        "SEED 系统通知", "这是一条由 seed 脚本发布的系统通知"
    )

    logger.success(
        f"[用户级互动] 收藏夹 {len(folder_ids)} 个，收藏 {fav_count} 次，关注/拉黑/事件/通知已覆盖"
    )


async def _fetch_lottery_ids(n: int = 1) -> list[int]:
    """从 dyndetail.lotdata 直连取真实 lottery_id（与 mysql_message_url 同 MySQL 实例）。

    lottery 资源由 crawler RPC 校验存在性，其底层即读此表；seed 直连取一个真实
    lottery_id 作为「资源」做点赞/评论/@ 联调。失败（库/表不存在、无数据）降级返回空列表。
    """
    try:
        conn = await aiomysql.connect(**_raw_conn("dyndetail"))
        try:
            cur = await conn.cursor()
            await cur.execute(
                "SELECT lottery_id FROM lotdata ORDER BY lottery_time DESC LIMIT %s", (n,)
            )
            rows = await cur.fetchall()
        finally:
            conn.close()
        return [int(r[0]) for r in rows]
    except Exception as e:  # noqa: BLE001
        logger.warning(f"直连 dyndetail.lotdata 取 lottery_id 失败（降级空）: {e}")
        return []


async def _fetch_lottery_ids_via_api(
    crawler_base_url: str, n: int = 1
) -> list[int]:
    """调 crawler 的 GetAllLottery HTTP 接口取真实 lottery_id（走接口，不直连库）。

    路径：``{crawler_base_url}/api/v1/lottery_database/bili/GetAllLottery`` (POST)。
    优先使用此方式；失败时由调用方降级到 ``_fetch_lottery_ids``（直连 dyndetail.lotdata）。
    """
    try:
        url = (
            f"{crawler_base_url.rstrip('/')}"
            "/api/v1/lottery_database/bili/GetAllLottery"
        )
        async with httpx.AsyncClient(timeout=10.0) as cli:
            resp = await cli.post(
                url, params={"page_num": 1, "page_size": max(1, n)}
            )
            resp.raise_for_status()
            payload = resp.json()
        data = (payload or {}).get("data") or {}
        ids: list[int] = []
        for key in ("common_lottery", "reserve_lottery", "official_lottery"):
            for item in data.get(key) or []:
                lid = item.get("lottery_id") if isinstance(item, dict) else None
                if lid is not None:
                    ids.append(int(lid))
        return ids[:n]
    except Exception as e:  # noqa: BLE001
        logger.warning(
            f"调 crawler GetAllLottery 接口取 lottery_id 失败（降级直连库）: {e}"
        )
        return []


def _build_resource_pool(
    normal_ids: list[int],
    lottery_ids: list[int],
    *,
    dyn_cap: int = _DYN_POOL_SIZE,
    lottery_cap: int = _LOTTERY_POOL_SIZE,
) -> list[tuple[int, InteractionBizTypeEnum]]:
    """把动态与 lottery 资源**交错**成统一互动池 ``[(oid, biz_type)]``。

    交错（``zip_longest``）而非拼接：保证两种 biz_type 在列表前后都有覆盖——
    拼接会让「前半段全是动态、后半段全是抽奖」，一旦中途失败或叠加 ``--skip-*``，
    某一种资源就完全没有数据。
    """
    dyn = [(int(i), InteractionBizTypeEnum.DYNAMIC) for i in normal_ids[:dyn_cap]]
    lot = [(int(i), InteractionBizTypeEnum.LOTTERY) for i in lottery_ids[:lottery_cap]]
    pool: list[tuple[int, InteractionBizTypeEnum]] = []
    for pair in zip_longest(dyn, lot):
        pool.extend(p for p in pair if p is not None)
    return pool


async def seed_lottery_resource(
    client: SeedClient,
    users: list[tuple[int, str | None]],
    crawler_base_url: str = "http://be-bilibili-crawler:23333",
) -> list[int]:
    """③-extra 取真实 lottery_id 并补上**通用资源独有的 LIKE 事件**缺口。

    lottery 的评论 / @ 已由 ``seed_comment`` 的混合资源池统一覆盖，此处只补齐
    动态点赞不会遇到的问题：通用资源点赞走 ``do_like_generic``，**后端不自动生成
    LIKE 事件**（动态点赞会生成），必须显式上报，否则消息中心「收到的赞」永远
    看不到抽奖资源的赞。事件统一落点同一测试用户，便于登录消息中心查看。

    Returns:
        真实 lottery_id 列表（供调用方并入混合资源池），取不到时为空列表。
    """
    if not users:
        return []
    lottery_ids = await _fetch_lottery_ids_via_api(crawler_base_url, _LOTTERY_POOL_SIZE)
    if not lottery_ids:
        lottery_ids = await _fetch_lottery_ids(
            _LOTTERY_POOL_SIZE
        )  # 接口不可达时直连库兜底
    if not lottery_ids:
        logger.warning("无可用 lottery_id（接口与直连库均失败），跳过 lottery 资源互动")
        return []
    lottery_id = lottery_ids[0]
    recipient = users[0][0]  # LIKE 事件统一落点（可登录查看消息中心）
    others = [u for u in users if u[0] != recipient] or users

    # 点赞（likeCount++；后端不自动生成事件 → 显式补 LIKE 事件给 recipient）
    for u in others[:3]:
        await client.thumb_lottery(u[0], lottery_id)
    if others:
        await client.report_event(
            recipient,
            InteractionActionTypeEnum.LIKE,
            lottery_id,
            others[0][0],
            others[0][1],
            str(lottery_id),
            biz_type=InteractionBizTypeEnum.LOTTERY,
        )

    logger.success(
        f"[lottery 资源互动] lottery_id={lottery_id}：点赞×{min(3, len(others))} + "
        f"LIKE 事件→{recipient} 已补发（评论/@ 由混合资源池统一覆盖）；"
        f"可登录 {recipient} 查看消息中心「收到的赞」并验证自动已读"
    )
    return lottery_ids


async def _load_block_relations(mids: list[int]) -> list[tuple[int, int]]:
    """只读探针：批量取这些用户**主动拉黑**的关系 ``[(mid, target_mid)]``。

    黑名单持久化且跨 seed 累积：每多一条，就多一个用户被静默排除在评论 /
    私信 / 关注 / @ 通知之外（服务端静默拒绝，seed 只能软降级跳过，
    互动覆盖逐轮变窄）。

    返回**有序**列表（同一 mid 内按 ``created_at`` 倒序、``target_mid`` 兜底），
    供 ``_normalize_blocklist`` 直接按序保留最新的几条。
    """
    if not mids:
        return []
    async with new_session() as s:
        rows = (
            await s.exec(
                select(UserFollow.mid, UserFollow.target_mid)
                .where(
                    col(UserFollow.mid).in_(mids),
                    col(UserFollow.status) == FollowStatusEnum.BLOCKED,
                )
                .order_by(
                    col(UserFollow.mid),
                    col(UserFollow.created_at).desc(),
                    col(UserFollow.target_mid),
                )
            )
        ).all()
    return [(int(m), int(t)) for m, t in rows]


def _is_dm_blocked(
    blocked: set[tuple[int, int]], sender_mid: int, receiver_mid: int
) -> bool:
    """``sender_mid`` 发给 ``receiver_mid`` 的私信是否会被黑名单拒绝。

    对齐服务端 ``FollowService.is_blocked_by(sender_mid, receiver_mid)`` 的
    **单向**判定：只有「接收方拉黑了发送方」才拒绝发送；反向拉黑不阻断本方向。
    """
    return (int(receiver_mid), int(sender_mid)) in blocked


async def _normalize_blocklist(
    client: SeedClient,
    users: list[tuple[int, str | None]],
    *,
    keep: int = 1,
) -> set[tuple[int, int]]:
    """把每个用户的主动黑名单收敛到最多 ``keep`` 条，其余走 unblock 接口解除。

    保留 ``keep`` 条用于验证「拉黑后不可互动」的负面场景，其余历史累积的黑名单
    会持续吃掉评论 / 私信 / 关注 / @ 通知的覆盖率，必须清理。
    **按 created_at 倒序保留最新的**——本次 seed 刚演示拉黑的那条必须留下，
    被清掉的应是更早的历史遗留。读走只读探针、写走业务接口
    ``POST /message/follow/unblock``，不直写主库。

    Returns:
        归一后**保留**的黑名单关系集合，供私信场景避让。
    """
    mids = [int(u[0]) for u in users]
    rows = await _load_block_relations(mids)
    if not rows:
        return set()
    kept: set[tuple[int, int]] = set()
    stale: list[tuple[int, int]] = []
    quota: dict[int, int] = {}
    for m, t in rows:
        if quota.get(m, 0) < keep:
            quota[m] = quota.get(m, 0) + 1
            kept.add((m, t))
        else:
            stale.append((m, t))
    for m, t in stale:
        try:
            await client.unblock(m, t)
        except RuntimeError as e:
            logger.warning(f"清理历史黑名单 {m}→{t} 失败（已跳过）: {e}")
    if stale:
        logger.success(
            f"[黑名单归一] 解除 {len(stale)} 条历史拉黑，每人保留 ≤{keep} 条（保留 {len(kept)} 条）"
        )
    else:
        logger.info(f"[黑名单归一] 无需清理，已有 {len(kept)} 条黑名单（每人 ≤{keep} 条）")
    return kept


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

    # 2) 通用互动计数：lottery 资源点赞（TInteractionStat）
    for mid, _ in users[: min(2, len(users))]:
        await client.thumb_lottery(mid, 10000000 + _rr.pick(range(90000000)))

    # 3) 用户空间举报
    await client.report_user(users[1][0], users[0][0])

    # 4) 封禁（root，comment 服务，临时 7 天）—— 封禁最后一个用户，演示后不影响主链路
    ban_target = users[-1][0]
    if ban_target != users[0][0]:
        try:
            await client.ban_user(ban_target)
        except RuntimeError as e:
            logger.warning(f"封禁失败: {e}")

    # 5) 头像审核流：提交头像（带 .jpg 后缀真实图源）→ 管理员审核通过
    avatar_mid = users[0][0]
    await client.submit_avatar(avatar_mid, _rr.pick(_IMG_URLS))
    await client.approve_avatar()

    logger.success(f"[消息与管理] 私信/通用计数/举报/封禁/头像审核流已覆盖")


async def seed(
    *,
    base_url: str,
    admin_mid: int,
    count: int,
    users_n: int,
    moment_concurrency: int,
    skip_moment: bool,
    skip_comment: bool,
    skip_interact: bool,
    skip_message: bool,
    skip_follow: bool,
    crawler_base_url: str = "http://be-bilibili-crawler:23333",
) -> None:
    # 素材池真实化：从 biliopusdb / bilidb 拉取真实素材（失败降级内置兜底）
    await _load_material_pools()
    real_users = await _fetch_real_users(users_n)
    if not real_users:
        logger.error("没有可用作作者的真实用户，终止。")
        return
    users = real_users[:users_n]

    async with SeedClient(base_url, admin_mid, req_concurrency=_SEED_REQ_CONCURRENCY) as client:
        normal_ids: list[int] = []

        if not skip_moment:
            normal_ids = await seed_moment(
                client, users, count, concurrency=moment_concurrency
            )
        # lottery 资源：取真实 lottery_id 并补 LIKE 事件（通用资源点赞后端不自动
        # 生成事件，动态点赞会生成）。返回的 id 并入下面的混合资源池，
        # 让评论 / at / 点赞一并覆盖到通用资源链路。
        lottery_ids: list[int] = []
        if not (skip_comment and skip_interact):
            lottery_ids = await seed_lottery_resource(
                client, users, crawler_base_url=crawler_base_url
            )

        if not skip_comment:
            await seed_comment(
                client, users, _build_resource_pool(normal_ids, lottery_ids)
            )
        if not skip_interact:
            await seed_interact(
                client,
                users,
                normal_ids or await _fallback_normal_ids(client),
                skip_follow=skip_follow,
            )
        # 黑名单归一：每人只保留 1 条（够验证「拉黑后不可互动」），
        # 历史累积的黑名单会持续吃掉评论 / 私信 / 关注 / @ 通知的覆盖率。
        # 归一结果透传给私信场景做配对避让。
        blocked = await _normalize_blocklist(client, users)
        if not skip_message:
            await seed_message(client, users, count, moment_concurrency, blocked)

        logger.success("全互动 seed 执行完成。")


async def _fallback_normal_ids(client: SeedClient) -> list[int]:
    """跳过动态模块时，从综合 Feed 拉取已过审动态作互动目标。"""
    try:
        data = await client._get(
            "/api/v1/community/feed/all", client.admin_mid, {"ps": 20, "sort": "time"}
        )
        return [int(it["dynId"]) for it in data.get("items", [])]
    except RuntimeError:
        return []


# ---------------------------------------------------------------------------
# 大数据灌数（基于 biliopusdb 真实动态，走 API）
# ---------------------------------------------------------------------------

BILIOPUS_DB = "biliopusdb"

# 点赞数分布（近似幂律：多数低赞、少数高赞），均值约 4 / 条
LIKE_DISTRIBUTION = [
    (0, 70),
    (1, 12),
    (3, 8),
    (8, 5),
    (20, 3),
    (50, 1.5),
    (120, 0.5),
]
# 浏览量分布（近似幂律），均值约 3 / 条
VIEW_DISTRIBUTION = [
    (0, 55),
    (1, 15),
    (3, 12),
    (8, 8),
    (20, 5),
    (60, 3),
    (150, 1.5),
    (400, 0.5),
]
# 评论数分布（近似幂律：多数无评论、少数多条），均值约 0.5 / 条
# 评论正文末尾追加随机 @，覆盖评论 @ 链路，并触发 REPLY（给动态作者）/ AT（给被 @ 用户）事件通知
COMMENT_DISTRIBUTION = [
    (0, 60),
    (1, 25),
    (2, 10),
    (4, 5),
]
# 话题关联概率（部分动态挂话题，压测 topic_feed 索引）
TOPIC_LINK_RATIO = 0.5
# 正文末尾追加随机 @ 的长度上限：动态正文上限 2000 字（MomentPublishService._CONTENT_MAX_LENGTH），
# 真实动态正文可能已接近上限，超长正文跳过 @，避免触发长度校验导致整条动态灌入失败
_BULK_AT_CONTENT_MAXLEN = 1900


def _raw_conn(db: str) -> dict:
    """从 mysql_message_url 派生指定库的连接参数（同一 MySQL 实例）。"""
    url = make_url(settings.mysql_message_url)
    return {
        "host": url.host or "127.0.0.1",
        "port": url.port or 10000,
        "user": url.username or "root",
        "password": url.password or "",
        "db": db,
    }


def _biliopus_conn() -> dict:
    """从 mysql_message_url 派生 biliopusdb 的连接参数（同一 MySQL 实例）。"""
    return _raw_conn(BILIOPUS_DB)


#: bilidb 库名（素材池话题名来源：t_topic_item）
BILIDB = "bilidb"


async def _load_material_pools() -> None:
    """从外库拉取真实素材池（幂等，启动时加载一次）。

    - `_SENTENCES` / `_COMMENTS` / `_REPLIES`：取自 `biliopusdb.t_lotdyninfo.dynContent`
      真实动态正文，按长度分池——长文（10~80 字）作动态正文 `_SENTENCES`，
      短文（2~30 字）作评论/楼中楼语 `_COMMENTS` / `_REPLIES`；统一去空白、去重。
    - `_TOPIC_NAMES`：取自 `bilidb.t_topic_item`（自动探测话题名列）。

    任一步失败（连接 / 表 / 列不存在）→ warning + 保留内置硬编码兜底，不阻断 seed。
    """
    global _SENTENCES, _COMMENTS, _REPLIES, _TOPIC_NAMES, _MATERIAL_LOADED
    if _MATERIAL_LOADED:
        return
    _MATERIAL_LOADED = True

    # 1) biliopusdb.t_lotdyninfo → 正文 / 评论 / 回复素材
    try:
        conn = await aiomysql.connect(**_biliopus_conn())
        try:
            cur = await conn.cursor()
            await cur.execute(
                "SELECT dynContent FROM t_lotdyninfo "
                "WHERE dynContent IS NOT NULL AND TRIM(dynContent) <> '' "
                "ORDER BY dynId DESC LIMIT 5000"
            )
            rows = await cur.fetchall()
        finally:
            conn.close()
        cleaned: list[str] = []
        for r in rows:
            t = re.sub(r"\s+", " ", str(r[0])).strip()
            if t and t not in cleaned:
                cleaned.append(t)
        sentences = [t for t in cleaned if 10 <= len(t) <= 80]
        shorts = [t for t in cleaned if 2 <= len(t) <= 30]
        if sentences:
            _SENTENCES = sentences
        if shorts:
            _COMMENTS = shorts
            _REPLIES = shorts
        logger.info(
            f"素材池已从 biliopusdb.t_lotdyninfo 加载：正文 {len(sentences)} / "
            f"评论语 {len(shorts)}（共 {len(cleaned)} 条去重）"
        )
    except Exception as e:  # noqa: BLE001
        logger.warning(f"biliopusdb 素材池加载失败，使用内置兜底: {e}")

    # 2) bilidb.t_topic_item → 话题名（自动探测话题名列）
    try:
        conn = await aiomysql.connect(**_raw_conn(BILIDB))
        try:
            cur = await conn.cursor()
            await cur.execute("SHOW COLUMNS FROM t_topic_item")
            cols = [r[0] for r in await cur.fetchall()]
            name_col = next(
                (
                    c
                    for c in cols
                    if c.lower()
                    in ("topic_name", "topicname", "name", "topic_title", "topic_text")
                ),
                None,
            )
            if name_col is None:
                raise RuntimeError(f"t_topic_item 未找到话题名列，实际列: {cols}")
            await cur.execute(
                f"SELECT DISTINCT `{name_col}` FROM t_topic_item "
                f"WHERE `{name_col}` IS NOT NULL AND TRIM(`{name_col}`) <> '' LIMIT 2000"
            )
            rows = await cur.fetchall()
        finally:
            conn.close()
        names = [str(r[0]).strip()[:20] for r in rows if r[0]]
        if names:
            _TOPIC_NAMES = list(dict.fromkeys(names))
        logger.info(f"话题名已从 bilidb.t_topic_item 加载：{len(_TOPIC_NAMES)} 个")
    except Exception as e:  # noqa: BLE001
        logger.warning(f"bilidb.t_topic_item 话题名加载失败，使用内置兜底: {e}")


def _sample(distribution: Sequence[tuple[int, float]]) -> int:
    """按分布**确定性轮遍**取值（distribution = [(值, 权重), ...]，取代随机加权采样）。

    权重可为小数，按 ×2 转整后做累计；每调用一次由全局 ``_rr`` 推进一格，命中对应累计区间，
    使整体分布比例≈权重且可复现（不再随机）。
    """
    scale = 2  # 权重里最多一位小数，×2 转整数
    items: list[tuple[int, int]] = []
    total = 0
    for value, weight in distribution:
        w = int(round(weight * scale))
        total += w
        items.append((value, w))
    bucket = _rr.pick(range(total))
    acc = 0
    for value, w in items:
        acc += w
        if bucket < acc:
            return value
    return items[-1][0]


_T = TypeVar("_T")


class _RoundRobin:
    """确定性轮遍游标：从 ``seq`` 轮流取下一项、取完回开头（single-queue 单线程安全）。

    用于取代 ``random.choice / random.sample / random.randint`` 的“随机挑”，
    让 seed 全流程**定值轮遍**（每个用户 / 每条素材都被均匀轮到、不重复）：
    - ``pick(seq)``：取下一项（返回元素类型同 ``seq``，兼容 list/range 等 Sequence）；
    - ``pick_n(seq, n)``：取连续 ``n`` 项（池足够大时不重复，循环后从头再来）。
    取空序列抛 ``ValueError``。
    """

    __slots__ = ("_i",)

    def __init__(self) -> None:
        self._i = 0

    def pick(self, seq: Sequence[_T]) -> _T:
        if not seq:
            raise ValueError("_RoundRobin.pick 从空序列取值")
        v = seq[self._i % len(seq)]
        self._i += 1
        return v

    def pick_n(self, seq: Sequence[_T], n: int) -> list[_T]:
        return [self.pick(seq) for _ in range(max(0, n))]

    def reset(self) -> None:
        self._i = 0


# 全 seed 共享的确定性轮遍游标（单事件循环顺序调用，跨阶段共用一条数据流）
_rr = _RoundRobin()


async def fetch_real_dyns(total: int) -> list[tuple]:
    """从 biliopusdb 流式拉取真实动态（keyset 分页，避免 OFFSET 深翻页慢）。

    仅取**话题与动态内容**相关字段，返回 (dynId, pubTime, dynContent, commentCount, repostCount)。
    不取外库作者 up_uid —— 灌入时作者统一从自有用户系统（pptr Postgres）随机映射。
    仅取 dynId>0 且 pubTime>2000-01-01 且正文非空的有效动态。
    """
    conn = await aiomysql.connect(**_biliopus_conn())
    out: list[tuple] = []
    try:
        cur = await conn.cursor()
        last_dyn_id: int | None = None
        page = 2000
        while len(out) < total:
            if last_dyn_id is None:
                await cur.execute(
                    "SELECT dynId, pubTime, dynContent, "
                    "COALESCE(commentCount,0), COALESCE(repostCount,0) "
                    "FROM t_lotdyninfo "
                    "WHERE dynId > 0 AND pubTime > '2000-01-01' "
                    "AND dynContent IS NOT NULL AND TRIM(dynContent) <> '' "
                    "ORDER BY dynId DESC LIMIT %s",
                    (page,),
                )
            else:
                await cur.execute(
                    "SELECT dynId, pubTime, dynContent, "
                    "COALESCE(commentCount,0), COALESCE(repostCount,0) "
                    "FROM t_lotdyninfo "
                    "WHERE dynId > 0 AND pubTime > '2000-01-01' "
                    "AND dynContent IS NOT NULL AND TRIM(dynContent) <> '' "
                    "AND dynId < %s "
                    "ORDER BY dynId DESC LIMIT %s",
                    (last_dyn_id, page),
                )
            rows = await cur.fetchall()
            if not rows:
                break
            out.extend(rows)
            last_dyn_id = rows[-1][0]
            logger.info(f"  已拉取真实动态 {len(out)} / {total}")
    finally:
        conn.close()
    return out[:total]


async def fetch_pptr_user_pool(size: int) -> list[tuple[int, str | None]]:
    """从自有用户系统（pptr Postgres）随机取 size 个真实用户 ``(uid, uname)``。

    用作动态作者 / 点赞者 / 浏览者池：外库动态仅保留话题与内容，
    所有者信息统一链接到本地用户系统，保证 Feed 回查 author 信息完整。
    昵称一并取出，供正文末尾追加随机 @ 使用（动态 AT 节点需要 ``name``）。
    """
    async with new_pptr_session() as s:
        stmt = (
            select(PptrUserInfo.uid, PptrUserDetail.uname)
            .join(
                PptrUserDetail,
                col(PptrUserDetail.mid) == col(PptrUserInfo.uid),
                isouter=True,
            )
            .where(col(PptrUserInfo.deletedAt).is_(None))
            .where(col(PptrUserDetail.uname).isnot(None))
            .order_by(func.random())
            .limit(size)
        )
        rows = (await s.exec(stmt)).all()
    users = [(int(uid), uname) for uid, uname in rows]
    if not users:
        logger.error(
            "pptr 库未取到任何真实用户 uid，无法作为动态作者，请确认 pptr 数据库连接与数据。"
        )
    return users


async def fetch_real_topics() -> list[str]:
    """拉取真实话题名（required_topic_text 非空，去重）。"""
    conn = await aiomysql.connect(**_biliopus_conn())
    try:
        cur = await conn.cursor()
        await cur.execute(
            "SELECT DISTINCT required_topic_text FROM t_lot_extra_info "
            "WHERE required_topic_text IS NOT NULL AND TRIM(required_topic_text) <> '' "
            "ORDER BY required_topic_text LIMIT 5000"
        )
        rows = await cur.fetchall()
    finally:
        conn.close()
    # 去除首尾空白（含全角/Unicode 空格，MySQL TRIM 不处理但 Python strip 能去），
    # 与 create_topic 接口对 topicName 的 strip 规范化保持一致，避免幂等复用比对失配。
    return list(dict.fromkeys(r[0].strip() for r in rows if r[0] and r[0].strip()))


async def _load_existing_topics() -> dict[str, int]:
    """直连 be-message MySQL 只读 TMomentTopic，建立 topicName->topicId 映射（幂等复用）。

    库里已有话题可能由不同 admin_mid 创建，``/topic/mine`` 按创建者过滤会漏掉，
    故直接读主库确认前置数据存在（只读不写），用于幂等复用，避免重复话题 422 后被丢弃导致动态无法挂话题。
    """
    url = make_url(settings.mysql_message_url)
    conn = await aiomysql.connect(
        host=url.host,
        port=url.port,
        user=url.username,
        password=url.password,
        db=url.database,
    )
    try:
        cur = await conn.cursor()
        await cur.execute("SELECT topicName, topicId FROM TMomentTopic")
        rows = await cur.fetchall()
    finally:
        conn.close()
    return {str(r[0]): int(r[1]) for r in rows}


async def _seed_topics(
    client: SeedClient,
    names: list[str],
    admin_mid: int,
    concurrency: int,
    existing: dict[str, int],
) -> list[int]:
    """并发创建并审核真实话题，复用已存在话题（幂等），返回 topicId 列表。

    ``existing`` 为 name->topicId 映射（预拉 + 创建时增量更新），用于幂等复用，
    避免重复话题返回 422 后被丢弃导致动态无法挂话题。
    """
    sem = asyncio.Semaphore(concurrency)

    async def one(name: str) -> int | None:
        name = name.strip()
        if not name:
            return None
        async with sem:
            # 1) 已存在则直接复用，否则创建
            if name in existing:
                tid = existing[name]
            else:
                try:
                    tid = await client.create_topic(admin_mid, name)
                    existing[name] = int(tid)
                    tid = int(tid)
                except Exception as e:  # noqa: BLE001
                    # 已存在（或响应丢失但后端已建）：回查全量复用
                    existing.update(await _load_existing_topics())
                    if name in existing:
                        tid = existing[name]
                    else:
                        logger.warning(f"话题「{name}」创建失败: {e}")
                        return None
            # 2) 确保审核通过：复用的 auditing 话题 / 新建话题都需 normal，否则动态关联会 400
            try:
                await client.approve_topic(tid)
            except Exception:  # noqa: BLE001
                pass  # 已是 normal（或状态异常），忽略
            return tid

    tasks = [asyncio.create_task(one(n)) for n in names]
    results: list[int | None] = []
    for f in tqdm(
        asyncio.as_completed(tasks),
        total=len(tasks),
        desc="seed topics",
        unit="个",
    ):
        try:
            results.append(await f)
        except Exception:  # noqa: BLE001
            pass
    return [t for t in results if isinstance(t, int)]


async def _seed_bulk_comment_suite(
    client: SeedClient,
    user_pool: list[tuple[int, str | None]],
    oid: int,
    up_mid: int,
    biz_type: InteractionBizTypeEnum,
    *,
    like_event_recipient: int | None = None,
) -> None:
    """大数据灌数：单个资源（动态 / lottery）的完整评论套件（软降级，单条失败不阻断）。

    覆盖：资源点赞（按 biz_type 分流 DYNAMIC→thumb / LOTTERY→thumb_lottery）、
    一级评论（正文末尾轮遍 @）、楼中楼（二级评论，带 @）、评论点赞/点踩、
    显式 @ 评论、举报、置顶；lottery 资源额外补发 LIKE 事件（通用资源点赞后端不自动
    生成事件，与 ``seed_lottery_resource`` 行为一致），统一落点 ``like_event_recipient``
    便于登录消息中心查看「收到的赞」。全流程**定值轮遍**（不再随机）。
    """
    if len(user_pool) < 2:
        return
    if up_mid and any(u[0] == up_mid for u in user_pool):
        commenters = [u for u in user_pool if u[0] != up_mid]
    else:
        # up_mid 不在用户池（lottery 等无作者资源）时轮遍指派一个资源作者
        up_mid = _rr.pick(user_pool)[0]
        commenters = [u for u in user_pool if u[0] != up_mid]
    if not commenters:
        return

    async def _safe(coro) -> None:
        """单条评论动作失败软降级跳过。"""
        try:
            await coro
        except Exception as e:  # noqa: BLE001
            logger.warning(f"灌数评论动作失败（已跳过）: {e}")

    async def _safe_add(coro):
        """评论发布失败软降级，返回 rpid 或 None。"""
        try:
            return await coro
        except Exception as e:  # noqa: BLE001
            logger.warning(f"灌数评论发布失败（已跳过）: {e}")
            return None

    def _rr_at(exclude_mid: int | None) -> list[tuple[int, str]]:
        """确定性轮遍 @ 目标：从 commenters（排除自己）轮流取 1 个。"""
        pool = [u for u in commenters if u[0] != exclude_mid]
        if not pool:
            return []
        mid, name = _rr.pick(pool)
        return [(int(mid), (name or f"user{mid}"))]

    # 0) 资源点赞（按 biz_type 分流）
    liker = _rr.pick(commenters)
    if biz_type is InteractionBizTypeEnum.LOTTERY:
        await _safe(client.thumb_lottery(liker[0], oid))
    else:
        await _safe(client.thumb(liker[0], oid))

    # 1) 一级评论（固定 2 条，轮遍取评论者）+ 审核通过（正文末尾轮遍 @，不 @ 自己）
    root_rpids: list[str] = []
    for commenter in _rr.pick_n(commenters, min(2, len(commenters))):
        rpid = await _safe_add(
            client.add_comment(
                commenter[0],
                oid,
                up_mid,
                biz_type=biz_type,
                at_users=_rr_at(commenter[0]),
            )
        )
        if not rpid:
            continue
        await _safe(client.approve_comment(rpid))
        root_rpids.append(rpid)

    # 2) 楼中楼（二级评论）：对每根一级评论固定回复 1 层，正文末尾轮遍 @
    for root_rpid in root_rpids:
        parent = root_rpid
        replier = _rr.pick(commenters)
        rpid = await _safe_add(
            client.add_comment(
                replier[0],
                oid,
                up_mid,
                biz_type=biz_type,
                root=root_rpid,
                parent=parent,
                message=_rr.pick(_REPLIES),
                at_users=_rr_at(replier[0]),
            )
        )
        if not rpid:
            continue
        await _safe(client.approve_comment(rpid))
        parent = rpid

    # 3) 评论点赞 / 点踩（落在一级评论上，赞/踩交替）
    for i, rpid in enumerate(root_rpids):
        actor = _rr.pick(commenters)
        await _safe(client.comment_action(actor[0], rpid, 1 if i % 2 == 0 else 2))

    # 4) 显式 @ 评论（@ + 末尾轮遍 @ 共存）
    if len(commenters) >= 2:
        at_target = commenters[0]
        at_name = at_target[1] or f"user{at_target[0]}"
        subject = "抽奖" if biz_type is InteractionBizTypeEnum.LOTTERY else "动态"
        await _safe(
            client.add_comment(
                commenters[1][0],
                oid,
                up_mid,
                biz_type=biz_type,
                message=f"@{at_name} 这个{subject}真不错",
                at_mids=[at_target[0]],
                at_name_to_mid={at_name: at_target[0]},
                at_users=_rr_at(commenters[1][0]),
            )
        )

    # 5) 举报一条评论
    if root_rpids:
        await _safe(client.report_comment(_rr.pick(commenters)[0], root_rpids[-1]))

    # 6) 评论置顶（资源作者身份）
    if root_rpids:
        await _safe(client.top_comment(up_mid, oid, root_rpids[-1], biz_type=biz_type))

    # lottery 资源点赞后端不自动生成 LIKE 事件 → 补发（与 seed_lottery_resource 一致）
    if biz_type is InteractionBizTypeEnum.LOTTERY and like_event_recipient is not None:
        actor = _rr.pick(commenters)
        await _safe(
            client.report_event(
                like_event_recipient,
                InteractionActionTypeEnum.LIKE,
                oid,
                actor[0],
                actor[1],
                str(oid),
                biz_type=InteractionBizTypeEnum.LOTTERY,
            )
        )


async def _seed_dynamic(
    client: SeedClient,
    user_pool: list[tuple[int, str | None]],
    topic_ids: list[int],
    real: tuple,
    sem: asyncio.Semaphore,
    author_cycle: "cycle[tuple[int, str | None]]",
) -> None:
    """单条动态：创建 → 审核通过 → 按分布点赞/评论/@/浏览（并发，全定值轮遍）。

    作者在 ``user_pool`` 上**轮流循环**（round-robin，``author_cycle``），
    保证每个用户都被轮到创建动态，总次数 = ``len(reals)``（= ``count``）。
    其余互动者（点赞 / 浏览 / @ / 评论）亦确定性轮遍（``_rr``），不再随机。

    正文末尾追加轮遍 @（不 @ 作者本人），让灌数数据同样覆盖动态 @ 链路；
    评论走 ``_seed_bulk_comment_suite``（一级评论 + 楼中楼 + 评论赞踩 + 显式 @ +
    举报 + 置顶，正文末尾轮遍 @），覆盖评论 @ 链路并触发 REPLY（给动态作者）/
    AT（给被 @ 用户）事件通知，使「收到的赞/回复/@」消息中心有真实数据；
    极长正文（已接近 2000 字上限）跳过 @，避免触发长度校验导致整条动态灌入失败。
    """
    async with sem:
        try:
            _dyn_id, _pub_time, content, _comment_count, _repost_count = real
            author, _ = next(author_cycle)
            nodes: list[dict] = [{"type": "WORDS", "text": content}]
            if len(content) <= _BULK_AT_CONTENT_MAXLEN:
                nodes.extend(_at_nodes(_at_targets_deterministic(user_pool, author)))
            # 话题：轮遍挂载（非概率）
            topic_id = _rr.pick(topic_ids) if topic_ids and _rr.pick(range(2)) == 0 else None
            new_dyn_id = await client.create_dynamic(
                author, scene="WORD", content=nodes, topic_id=topic_id
            )
            await client.approve(new_dyn_id)

            like_target = _sample(LIKE_DISTRIBUTION)
            view_target = _sample(VIEW_DISTRIBUTION)
            comment_target = _sample(COMMENT_DISTRIBUTION)
            tasks = []
            if like_target > 0 and user_pool:
                for u in _rr.pick_n(user_pool, min(like_target, len(user_pool))):
                    tasks.append(client.thumb(u[0], new_dyn_id))
            if view_target > 0 and user_pool:
                for u in _rr.pick_n(user_pool, min(view_target, len(user_pool))):
                    tasks.append(client.browse(u[0], new_dyn_id))

            # 评论套件：对动态跑完整评论链路（一级评论/@ + 楼中楼 + 评论赞踩 +
            # 显式 @ + 举报 + 置顶），生成 REPLY（动态作者）/ AT（被 @ 用户）事件，
            # 使「收到的赞/回复/@」消息中心有真实数据；单条失败不影响整体。
            # comment_target 为 0（多数动态无评论）时跳过整段评论，与分布一致。
            if comment_target > 0 and user_pool:
                tasks.append(
                    _seed_bulk_comment_suite(
                        client,
                        user_pool,
                        new_dyn_id,
                        author,  # up_mid：动态作者，接收 REPLY 事件
                        InteractionBizTypeEnum.DYNAMIC,
                    )
                )

            if tasks:
                # 单条动态的点赞/评论/@/浏览并发发起，单条失败不影响整体
                await asyncio.gather(*tasks, return_exceptions=True)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"动态灌入失败（跳过）: {e}")


async def _seed_bulk_dm(
    client: SeedClient,
    users: list[tuple[int, str | None]],
    count: int,
    dm_concurrency: int,
    blocked: set[tuple[int, int]] | None = None,
) -> None:
    """大数据灌数：在 pptr 用户池上批量互发私信，填充 DM 内容（软降级跳过业务拒绝）。

    复用与 ``seed_message`` 场景 E 一致的 O(n²) 遍历逻辑：取前 ``count`` 个有序用户对
    （剔除「接收方已拉黑发送方」方向），每对发 1 条并审核通过。覆盖「用户对网络」广度，
    使 pptr 用户池内任意用户（如登录查看 demo 的测试账号）都有私信内容可读。
    接收方关闭陌生人私信时，来自陌生人的消息被过滤（仅发送方自见），此处宽容跳过该对，
    不阻断整体；发送方自己的消息始终可见，故发起方视角必有私信内容。
    """
    if len(users) < 2:
        logger.warning("用户数不足，跳过大数据灌数私信。")
        return
    blocked = blocked or set()
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
            desc="bulk dm",
            unit="条",
        ):
            try:
                if await f:
                    total += 1
            except Exception:  # noqa: BLE001
                pass
    logger.success(
        f"[大数据灌数] 私信填充完成：遍历用户对取前 {len(directed)} 对（每对 1 条），"
        f"实际发送 {total} 条（目标 {count} 条）"
    )


async def run_bulk(args: argparse.Namespace) -> None:
    # 素材池真实化：从 biliopusdb / bilidb 拉取真实素材（失败降级内置兜底）
    await _load_material_pools()
    logger.info(
        f"灌数计划（走 API）：count={args.count}, base_url={args.base_url}, "
        f"concurrency={args.concurrency}, dry_run={args.dry_run}"
    )
    if args.count < 1:
        logger.error("count 必须 >= 1")
        sys.exit(2)

    # 1. 拉取数据源（只读 biliopusdb / pptr，不写主库）
    logger.info("拉取 biliopusdb 真实数据…")
    reals = await fetch_real_dyns(args.count)
    if not reals:
        logger.error("biliopusdb 无有效数据，中止")
        sys.exit(1)
    topic_names = await fetch_real_topics()
    logger.info(f"真实动态 {len(reals)} 条 / 真实话题 {len(topic_names)} 个")

    if args.dry_run:
        like_total = sum(_sample(LIKE_DISTRIBUTION) for _ in reals)
        view_total = sum(_sample(VIEW_DISTRIBUTION) for _ in reals)
        comment_total = sum(_sample(COMMENT_DISTRIBUTION) for _ in reals)
        logger.info(
            f"[dry-run] 将经 API 灌入：动态 {len(reals)}、话题 {len(topic_names)}、"
            f"点赞约 {like_total}、一级评论/@约 {comment_total}（每条再附带楼中楼/赞踩/"
            f"举报/置顶，触发 REPLY/AT 事件）、浏览约 {view_total}；"
            f"另取 {_LOTTERY_POOL_SIZE} 个真实 lottery_id 跑等价评论套件（含资源点赞补 LIKE 事件）；"
            f"不调用接口"
        )
        return

    # 作者 / 点赞者 / 浏览者 / @对象统一取自自有用户系统（pptr Postgres）。
    user_pool = await fetch_pptr_user_pool(args.users_pool_size)
    if not user_pool:
        logger.error("pptr 无可用用户 uid，无法映射动态作者，中止")
        sys.exit(1)
    logger.info(f"自有用户池（pptr）{len(user_pool)} 个，用作作者/点赞者/浏览者/@对象")

    async with SeedClient(
        args.base_url, args.admin_mid, req_concurrency=_SEED_REQ_CONCURRENCY
    ) as client:
        # 2. 话题：预拉已有话题（直读主库幂等），并发创建 + 审核通过
        logger.info("预拉已有话题（直读主库，幂等）…")
        existing_topics = await _load_existing_topics()
        logger.info(f"  已有话题 {len(existing_topics)} 个")
        logger.info("创建并审核话题…")
        topic_ids = await _seed_topics(
            client, topic_names, args.admin_mid, args.concurrency, existing_topics
        )
        logger.info(f"  话题 {len(topic_ids)} 个（含复用已有）")

        # 3. 动态：并发创建 + 审核 + 点赞/浏览（正文末尾随机 @）
        # 作者在 user_pool 上轮流循环（round-robin），每个用户都轮到创建，总次数 = len(reals)
        logger.info("经 API 灌入动态（含点赞/浏览/@）…")
        sem = asyncio.Semaphore(args.concurrency)
        author_cycle = cycle(user_pool)
        tasks = [
            asyncio.create_task(
                _seed_dynamic(
                    client,
                    user_pool,
                    topic_ids,
                    real,
                    sem,
                    author_cycle,
                )
            )
            for real in reals
        ]
        for f in tqdm(
            asyncio.as_completed(tasks), total=len(tasks), desc="seed moments"
        ):
            try:
                await f
            except Exception as e:  # noqa: BLE001
                logger.warning(f"单条动态任务异常（已跳过）: {e}")

        # 4. lottery 资源：取真实 lottery_id，跑与动态等价的完整评论套件
        #    （一级评论/@ + 楼中楼 + 评论赞踩 + 显式 @ + 举报 + 置顶 + 资源点赞
        #    补 LIKE 事件），补齐「大数据灌数」阶段对通用资源评论链路的覆盖。
        logger.info("经 API 灌入 lottery 资源评论套件…")
        lottery_ids = await _fetch_lottery_ids_via_api(
            args.crawler_base_url, _LOTTERY_POOL_SIZE
        )
        if not lottery_ids:
            lottery_ids = await _fetch_lottery_ids(_LOTTERY_POOL_SIZE)
        if lottery_ids and user_pool:
            like_recipient = user_pool[0][0]  # LIKE 事件统一落点（可登录查看消息中心）
            lot_tasks = [
                _seed_bulk_comment_suite(
                    client,
                    user_pool,
                    int(lid),
                    _rr.pick(user_pool)[0],  # lottery 无作者，轮遍指派资源作者
                    InteractionBizTypeEnum.LOTTERY,
                    like_event_recipient=like_recipient,
                )
                for lid in lottery_ids
            ]
            for f in tqdm(
                asyncio.as_completed(lot_tasks),
                total=len(lot_tasks),
                desc="seed lottery",
                unit="个",
            ):
                try:
                    await f
                except Exception as e:  # noqa: BLE001
                    logger.warning(f"lottery 资源灌数失败（已跳过）: {e}")
            logger.info(
                f"  lottery 资源 {len(lottery_ids)} 个评论套件完成"
                f"（点赞 + LIKE 事件→{like_recipient} 已覆盖）"
            )
        else:
            logger.warning(
                "无可用 lottery_id（接口与直连库均失败），跳过 lottery 资源灌数"
            )

        # 5. 私信：在 pptr 用户池上批量互发，填充 DM 内容（含 127763472 等任意 pptr 用户）。
        #    全互动阶段仅在随机 12 人子集上跑私信，大数据灌数阶段用完整 pptr 池补覆盖，
        #    使登录 demo 的测试账号也能看到私信内容（发送方自己的消息始终可见）。
        if user_pool:
            logger.info("经 API 灌入私信（pptr 用户池批量互发）…")
            await _seed_bulk_dm(
                client,
                user_pool,
                args.count,
                args.concurrency,
            )
        else:
            logger.warning("无可用用户池，跳过大数据灌数私信")

    logger.info("灌数完成！浏览计数由热路径原子 ±1 维护（2.42.0 起对账脚本已移除，浏览明细每用户每资源一行）。")


# ---------------------------------------------------------------------------
# 统一 CLI 入口：一个命令跑完全互动联调 + 大数据灌数
# ---------------------------------------------------------------------------


def _run_full_args(args: argparse.Namespace) -> dict:
    return dict(
        base_url=args.base_url,
        admin_mid=args.admin_mid,
        count=args.full_count,
        users_n=args.full_users,
        moment_concurrency=args.full_concurrency,
        skip_moment=False,
        skip_comment=False,
        skip_interact=False,
        skip_message=False,
        skip_follow=args.skip_follow,
        crawler_base_url=args.crawler_base_url,
    )


def _bulk_namespace(args: argparse.Namespace) -> argparse.Namespace:
    """把统一 CLI 的参数映射成 run_bulk 所需的 namespace。"""
    ns = argparse.Namespace()
    ns.count = args.full_count
    ns.base_url = args.base_url
    ns.admin_mid = args.admin_mid
    # 大数据灌数统一复用 --full-count（已删除独立的 --bulk-count）与 --full-concurrency（已删除独立的 --bulk-concurrency）
    ns.concurrency = args.full_concurrency
    ns.users_pool_size = args.bulk_users_pool
    ns.dry_run = args.dry_run
    ns.crawler_base_url = args.crawler_base_url
    return ns


async def _run_full(args: argparse.Namespace) -> None:
    """阶段一：全互动联调（覆盖 4 大类 18 项，断言失败响亮报错）。"""
    logger.info("========== 阶段一：全互动联调 ==========")
    if args.dry_run:
        # --dry-run 必须在**任何接口调用之前**短路：本阶段写的是 MySQL 主库真实
        # 业务数据（动态 / 评论 / 私信 / 关注关系），一旦跑起来只能靠 Ctrl-C 中断，
        # 已落库的数据无法自动回滚。
        logger.info(
            f"[dry-run] 阶段一将执行：发布并审核动态 {args.full_count} 条"
            f"（并发 {args.full_concurrency}），用户池 {args.full_users} 人；"
            f"评论体系在「动态 {_DYN_POOL_SIZE} + lottery {_LOTTERY_POOL_SIZE}」"
            f"混合资源池上跑（一级评论/楼中楼/赞踩/@/举报/置顶）；"
            f"随后关注+拉黑（每人归一保留 1 条黑名单）、私信双向全流程、"
            f"通用计数 / 举报 / 封禁 / 头像审核。本阶段不调用接口。"
        )
        return
    await seed(**_run_full_args(args))


async def _run_bulk(args: argparse.Namespace) -> None:
    """阶段二：基于真实动态的大数据灌数（走 API，软降级）。"""
    logger.info("========== 阶段二：大数据灌数 ==========")
    try:
        await run_bulk(_bulk_namespace(args))
    except KeyboardInterrupt:
        raise
    except Exception as e:  # noqa: BLE001
        # 大数据灌数失败（biliopusdb/连接问题）软降级，不阻断整体
        logger.error(f"大数据灌数阶段失败（已跳过）: {e}")


async def run_all(args: argparse.Namespace) -> None:
    # seed 默认安静：只输出「真正未知/失败」的 ERROR 级日志。
    # info/success（过程提示）与 warning（预期软降级 / 业务拒绝，如拉黑、资源不存在、
    # 评论被拒、转发失败等）一律不打印；进度由 tqdm 展示。真正异常走 logger.error / 崩溃报错。
    logger.remove()
    logger.add(sys.stderr, level="ERROR")
    if not args.skip_full:
        await _run_full(args)
    if not args.skip_bulk:
        await _run_bulk(args)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="统一 seed 入口（单文件）：一个命令跑完全互动联调 + 大数据灌数"
    )
    p.add_argument(
        "--base-url", default="http://127.0.0.1:18739", help="be-message 服务地址"
    )
    p.add_argument(
        "--admin-mid", type=int, default=11, help="role=root 管理员 mid（全场景共用）"
    )
    # 全互动阶段参数
    p.add_argument(
        "--full-count",
        type=int,
        default=1000,
        help="全互动动态条数与大数据灌数条数统一（默认 1000）",
    )
    p.add_argument(
        "--full-users", type=int, default=12, help="全互动阶段取真实用户数（默认 12）"
    )
    p.add_argument(
        "--full-concurrency",
        type=int,
        default=50,
        help="全互动动态创建与大数据灌数统一并发数（默认 50，asyncio.Semaphore 控制）",
    )
    p.add_argument(
        "--skip-follow", action="store_true", help="全互动阶段跳过关注流验证"
    )
    # 大数据灌数阶段参数
    p.add_argument(
        "--bulk-users-pool",
        type=int,
        default=20000,
        help="大数据灌数用户池大小（默认 20000）",
    )
    # 阶段开关
    p.add_argument(
        "--skip-full", action="store_true", help="跳过全互动阶段（只跑大数据灌数）"
    )
    p.add_argument(
        "--skip-bulk", action="store_true", help="跳过大数据灌数阶段（只跑全互动）"
    )
    p.add_argument(
        "--crawler-base-url",
        default="http://be-bilibili-crawler:23333",
        help="crawler HTTP 服务地址（用于 GetAllLottery 接口取真实 lottery_id）",
    )
    p.add_argument("--dry-run", action="store_true", help="只打印计划，不调用接口")
    return p


def main() -> None:
    args = build_parser().parse_args()
    try:
        asyncio.run(run_all(args))
    except KeyboardInterrupt:
        logger.warning("用户中断")
        sys.exit(1)


if __name__ == "__main__":
    main()
