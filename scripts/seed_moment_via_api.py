"""动态（Moment）模块测试数据种子脚本 —— **纯 HTTP 接口调用版**。

不直接写数据库，全部通过 be-message-service 的 HTTP 接口走真实业务链路，
用于联调 / 模拟生产环境，同时验证各接口功能是否正常：

1. ``POST /api/v1/moment/create``         作者身份发布动态（WORD / FORWARD，落 auditing）
2. ``POST /api/v1/moment/audit/approve``   管理员(role=root) 审核通过 → normal
3. ``POST /api/v1/moment/thumb``           其他用户点赞（TMomentLike 明细 + likeCount 原子 +1）
4. ``POST /api/v1/comment/add``            其他用户评论（复用评论区，type=DYNAMIC）
5. ``POST /api/v1/moment/repost``          作者转发已审核通过的 normal 动态
6. ``POST /api/v1/message/follow/do``      建立关注关系（供 /feed/following 关注流验证）
7. ``GET  /api/v1/moment/feed/following``  验证关注流（关注的人发布的动态）

用法（项目根目录下，uv 管理环境，需先启动 be-message-service）：
    # 塞 40 条动态、取 12 个真实用户作作者、额外生成 200 条评论与若干点赞/转发
    uv run python scripts/seed_moment_via_api.py --count 40 --users 12

    # 只生成，不跑关注流验证
    uv run python scripts/seed_moment_via_api.py --count 40 --skip-follow

    # 指定服务地址（默认 http://127.0.0.1:18739）
    uv run python scripts/seed_moment_via_api.py --base-url http://localhost:18739 --count 20

说明：
- 作者/点赞者/评论者均取自 pptr Postgres 的真实用户（只读回查，不写 pptr）；
- 审核通过使用 role=root 的管理员（默认 mid=11，可 --admin-mid 覆盖）；
- 纯接口调用，无任何对 be-message MySQL 主库的直接写操作。
"""

from __future__ import annotations

import argparse
import asyncio
import random
import sys

import httpx
from loguru import logger
from sqlalchemy import func, select
from sqlmodel import col

from app.core.database import new_pptr_session
from app.models.pptr_db import PptrUserDetail, PptrUserInfo

# ---------------------------------------------------------------------------
# 真实感素材池
# ---------------------------------------------------------------------------

_SENTENCES = [
    "今天天气真好，出门溜达了一圈，随手记录一下～",
    "刚看完一个新番，剧情太顶了，强烈安利给各位！",
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

_TOPIC_NAMES = ["日常", "美食", "动漫", "编程", "健身", "旅行", "摄影", "读书"]

_IMG_URLS = [
    "https://picsum.photos/seed/bili1/600/400",
    "https://picsum.photos/seed/bili2/600/400",
    "https://picsum.photos/seed/bili3/600/400",
    "https://picsum.photos/seed/bili4/600/400",
]


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


def _content_nodes(sentence: str) -> list[dict]:
    """构造富文本节点（WORDS + 30% 概率外链图片 LINK）。"""
    nodes: list[dict] = [{"type": "WORDS", "text": sentence}]
    if random.random() < 0.3:
        url = random.choice(_IMG_URLS)
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
            .join(PptrUserDetail, PptrUserDetail.mid == PptrUserInfo.uid, isouter=True)
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


class SeedClient:
    """薄封装：以指定用户身份调 be-message HTTP 接口。"""

    def __init__(self, base_url: str, admin_mid: int) -> None:
        self.base = base_url.rstrip("/")
        self.admin_mid = admin_mid
        self.client = httpx.AsyncClient(timeout=30)

    async def __aenter__(self) -> "SeedClient":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.client.aclose()

    async def _post(
        self, path: str, mid: int, body: dict, *, role: str = "normal"
    ) -> dict:
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

    async def _get(self, path: str, mid: int, params: dict | None = None) -> dict:
        resp = await self.client.get(
            f"{self.base}{path}", params=params, headers=_headers(mid)
        )
        if resp.status_code != 200:
            raise RuntimeError(
                f"GET {path} 非 200: {resp.status_code} {resp.text[:300]}"
            )
        payload = resp.json()
        if payload.get("code", 0) != 0:
            raise RuntimeError(f"GET {path} 业务失败: {payload}")
        return payload.get("data") or {}

    # ---- 业务动作 ----

    async def _req(self, factory, label: str, *, retries: int = 3) -> dict:
        """执行一个请求：``factory()`` 返回协程，超时/连接错误自动重试
        （每次重试重新创建协程；调试模式事件循环忙时更稳）。"""
        last: Exception | None = None
        for attempt in range(1, retries + 1):
            try:
                return await asyncio.wait_for(factory(), timeout=60)
            except (asyncio.TimeoutError, httpx.TimeoutException) as e:
                last = e
                if attempt < retries:
                    logger.warning(f"{label} 第 {attempt} 次超时，重试…")
                    await asyncio.sleep(1.0)
        raise RuntimeError(f"请求超时(60s×{retries}): {label}: {last}")

    async def create_dynamic(
        self, mid: int, *, scene: str, content: list[dict], topic_id: int | None = None
    ) -> int:
        body: dict = {"scene": scene, "content": content}
        if topic_id is not None:
            body["topic"] = {"topicId": topic_id}
        data = await self._req(
            lambda: self._post("/api/v1/moment/create", mid, body),
            f"create mid={mid}",
        )
        return int(data["dynId"])

    async def approve(self, dyn_id: int) -> None:
        await self._req(
            lambda: self._post(
                "/api/v1/moment/audit/approve",
                self.admin_mid,
                {"dynId": dyn_id, "remark": "SEED script approve"},
                role="root",
            ),
            f"approve dyn={dyn_id}",
        )

    async def repost(self, mid: int, src_dyn_id: int, content: list[dict]) -> int:
        data = await self._req(
            lambda: self._post(
                "/api/v1/moment/repost",
                mid,
                {"srcDynId": src_dyn_id, "content": content},
            ),
            f"repost mid={mid} src={src_dyn_id}",
        )
        return int(data["dynId"])

    async def thumb(self, mid: int, dyn_id: int) -> None:
        await self._req(
            lambda: self._post("/api/v1/moment/thumb", mid, {"dynId": dyn_id, "up": 1}),
            f"thumb mid={mid} dyn={dyn_id}",
        )

    async def add_comment(self, mid: int, dyn_id: int, author_mid: int) -> str:
        """发评论并返回 rpid（评论系统开启先审后发，正常会落 auditing）。"""
        data = await self._req(
            lambda: self._post(
                "/api/v1/comment/add",
                mid,
                {
                    "oid": str(dyn_id),
                    "type": "dynamic",
                    "message": random.choice(_COMMENTS),
                    "up_mid": author_mid,
                },
            ),
            f"comment mid={mid} dyn={dyn_id}",
        )
        return data.get("rpid") or ""

    async def approve_comment(self, rpid: str) -> None:
        """管理员审核通过一条评论（op=pass，root）→ NORMAL 对外可见、计入动态评论数。"""
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

    async def follow(self, mid: int, target_mid: int) -> None:
        await self._req(
            lambda: self._post(
                "/api/v1/message/follow/do", mid, {"target_mid": target_mid}
            ),
            f"follow {mid}->{target_mid}",
        )

    async def topic_ids(self, mid: int) -> list[int]:
        """取话题广场现有话题 id（若话题为空则返回空，不强制建话题）。"""
        data = await self._get("/api/v1/moment/topic/square", mid, {"page": 1, "page_size": 50})
        return [t["topicId"] for t in data.get("items", [])]

    async def feed_following(self, mid: int) -> dict:
        return await self._get("/api/v1/moment/feed/following", mid, {"page": 1, "page_size": 50})


async def seed(
    *,
    base_url: str,
    admin_mid: int,
    count: int,
    users_n: int,
    skip_follow: bool,
) -> None:
    real_users = await _fetch_real_users(users_n)
    if not real_users:
        logger.error("没有可用作作者的真实用户，终止。")
        return
    authors = real_users[:users_n]

    async with SeedClient(base_url, admin_mid) as client:
        # 话题：拉取现有话题 id 备用（不创建）
        topic_ids = await client.topic_ids(authors[0][0])
        logger.info(f"可用话题 {len(topic_ids)} 个: {topic_ids[:5]}{'…' if len(topic_ids) > 5 else ''}")

        normal_ids: list[int] = []  # 已审核通过的动态，供转发 / 点赞 / 评论
        forward_ids: list[int] = []
        comment_count = 0
        like_count = 0

        for i in range(count):
            author_mid, author_name = random.choice(authors)
            sentence = random.choice(_SENTENCES)
            topic_id = random.choice(topic_ids) if topic_ids and random.random() < 0.5 else None

            # 1) 发布
            dyn_id = await client.create_dynamic(
                author_mid,
                scene="WORD",
                content=_content_nodes(sentence),
                topic_id=topic_id,
            )
            # 2) 审核通过 → normal
            await client.approve(dyn_id)
            normal_ids.append(dyn_id)
            logger.debug(f"[{i + 1}/{count}] 作者 {author_mid} 发布并过审 dynId={dyn_id}")

            # 3) 点赞：随机 N 个其他用户点赞（走 thumb 接口，幂等 + likeCount 原子 +1）
            likers = [u for u in real_users if u[0] != author_mid]
            for liker in random.sample(likers, min(random.randint(0, 5), len(likers))):
                await client.thumb(liker[0], dyn_id)
                like_count += 1

            # 4) 评论：随机 N 个用户评论（复用评论区 type=DYNAMIC），
            #    评论系统开启先审后发 → 落 auditing，需 root 审核通过（op=pass）
            #    才对外可见并计入动态评论数（TMomentStat.commentCount +1）
            for commenter in random.sample(likers, min(random.randint(0, 3), len(likers))):
                rpid = await client.add_comment(commenter[0], dyn_id, author_mid)
                await client.approve_comment(rpid)
                comment_count += 1
                await asyncio.sleep(0.05)

            # 5) 转发：已过审动态中 ~20% 被转发（走 repost + 再次审核）
            if normal_ids and random.random() < 0.2:
                src_dyn = random.choice(normal_ids)
                fwd_id = await client.repost(
                    author_mid, src_dyn, _content_nodes("转发：这个说得太对了")
                )
                await client.approve(fwd_id)
                forward_ids.append(fwd_id)

            if (i + 1) % 10 == 0:
                logger.info(f"已处理 {i + 1}/{count} 条动态")

        logger.success(
            f"动态生成完成：{count} 条（含转发 {len(forward_ids)}），"
            f"点赞 {like_count} 次，评论 {comment_count} 条。"
        )

        # 6) 关注关系：让「第一个作者」关注其他部分作者，验证关注流
        if not skip_follow:
            hub_mid = authors[0][0]
            others = [m for m, _ in authors[1:] if m != hub_mid]
            for target in others[: min(5, len(others))]:
                try:
                    await client.follow(hub_mid, target)
                except RuntimeError as e:
                    logger.warning(f"关注 {hub_mid}→{target} 失败: {e}")
            # 7) 验证关注流：hub 的关注流应返回其关注作者的动态
            feed = await client.feed_following(hub_mid)
            items = feed.get("items", [])
            feed_mids = {it["mid"] for it in items}
            logger.info(
                f"关注流验证：{hub_mid} 关注 {len(others[:5])} 人，"
                f"/feed/following 返回 {len(items)} 条（作者 {sorted(feed_mids)[:10]}）"
            )


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Moment 模块测试数据种子脚本（纯 HTTP 接口版）")
    p.add_argument("--base-url", default="http://127.0.0.1:18739", help="be-message 服务地址")
    p.add_argument("--admin-mid", type=int, default=11, help="role=root 管理员 mid（审核通过用）")
    p.add_argument("--count", type=int, default=40, help="生成动态条数（默认 40）")
    p.add_argument("--users", type=int, default=12, help="取多少个真实用户作作者（默认 12）")
    p.add_argument("--skip-follow", action="store_true", help="跳过关注关系建立与关注流验证")
    return p.parse_args()


async def main() -> None:
    args = parse_args()
    await seed(
        base_url=args.base_url,
        admin_mid=args.admin_mid,
        count=args.count,
        users_n=args.users,
        skip_follow=args.skip_follow,
    )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.warning("用户中断")
        sys.exit(1)
