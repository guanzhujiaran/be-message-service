"""动态正文首图提取（共享工具）。

原 `_resolve_source_meta` 的标题 / 封面回捞逻辑已统一下沉到各互动资源类
（`app/services/interaction_actions/*/biz.py` 的 `check_exists()` / `_load_meta()`，
由 `BaseBiz.get_resource()` 统一装配为 `InteractionResource`，见计划书 §5.11 / C20）。
本模块仅保留从动态富文本 `contentJson` 取首图的纯函数，供 `DynamicBiz` 复用。
"""
from __future__ import annotations

from app.models.db.moment_tbl import TMoment


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


__all__ = ["_first_pic", "TMoment"]
