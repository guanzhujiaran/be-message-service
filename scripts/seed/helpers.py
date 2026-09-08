"""通用构造工具：网关身份请求头、@ 文本 / AT 节点、富文本正文节点。"""
from .material import _IMG_URLS
from .rr import _rr

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
