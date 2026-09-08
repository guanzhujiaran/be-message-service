"""素材池（动态正文 / 评论语 / 图片 / 话题名）及其外库加载。

池内容优先由 ``load_material_pools()`` 从 biliopusdb / bilidb 覆盖，
任一外部数据源失败 → warning + 保留内置兜底，不阻断 seed。
"""
import re

import aiomysql
from loguru import logger

from .datasource import BILIDB, _biliopus_conn, _raw_conn

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
