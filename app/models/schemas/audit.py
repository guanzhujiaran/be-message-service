"""管理端审核项的「内容来源」模型。

审核队列里只有 `rpid` / `msgkey` 这类裸 ID，管理员无法判断这条内容出自哪里，
更没法快速回到原始现场核对上下文。`AuditSourceInfo` 把来源统一收敛成一个结构：

- `label`        ：人类可读的来源名（前端直接展示成可点击文案）；
- `url`          ：站内相对路径，前端 `router.push` 即可跳转；
- `external_url` ：站外原始内容链接（B 站动态 / 专栏等），前端新开标签页；
- `params`       ：附加定位参数（rpid / msgkey / session_key），供前端高亮或深链。

`url` 与 `external_url` 至少有一个非空时前端才渲染成可点击链接，
两者皆空表示该来源暂无可跳转的落地页（仅展示 label）。
"""

from sqlmodel import Field, SQLModel


class AuditSourceInfo(SQLModel):
    """审核项的内容来源（供管理端点击直达原始内容）。"""

    kind: str = Field(description="来源大类：comment 评论 / dm 私信")
    biz_type: str = Field(
        description="业务类型：评论为 CommentTypeEnum，私信固定为 dm"
    )
    label: str = Field(description="来源展示文案，如「抽奖卡片 #123」")
    oid: str | None = Field(default=None, description="业务实体id（字符串），私信为空")
    up_mid: int | None = Field(default=None, description="内容作者mid（评论区 UP 主）")
    url: str | None = Field(
        default=None, description="站内相对路径，前端可直接 router.push 跳转"
    )
    external_url: str | None = Field(
        default=None, description="站外原始内容链接（B 站动态 / 专栏）"
    )
    params: dict[str, str] = Field(
        default_factory=dict, description="附加定位参数（rpid / msgkey / session_key）"
    )


__all__ = ["AuditSourceInfo"]
