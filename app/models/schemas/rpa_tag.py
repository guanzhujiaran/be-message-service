"""RPA 资源标签（rpa_tag）用户侧接口的数据模型（2.49.0）。

be-message 全托管：创建/打标/列表经 RPC 调用 RPA 存储（见
`docs/rpa-tag-be-message-biz-计划书.md`），审核走通用
`/api/v1/audit/approve|reject`（bizType=RPA_TAG）。本模块只承载
be-message 侧的请求/响应模型，标签实体本身仍存于 RPA 侧 `rpa_tag` 表。
"""

from sqlmodel import SQLModel, Field

from typing import Optional


class RpaTagCreateReq(SQLModel):
    """创建标签请求（登录用户）→ 进入待审核 `auditing`。"""

    name: str = Field(description="标签名称（唯一）")
    color: str = Field(default="#409EFF", description="标签颜色（十六进制）")


class RpaTagAttachReq(SQLModel):
    """为资源关联标签请求（登录用户）；仅可关联 `normal` 标签。"""

    tagId: int = Field(description="标签 id")
    targetType: str = Field(description="目标资源类型：action / workflow / plugin")
    targetId: str = Field(description="目标资源 id（字符串）")


class RpaTagDetachReq(SQLModel):
    """移除资源上的标签请求（登录用户）。"""

    tagId: int = Field(description="标签 id")
    targetType: str = Field(description="目标资源类型")
    targetId: str = Field(description="目标资源 id（字符串）")


class RpaTagListReq(SQLModel):
    """列出标签请求。普通用户仅 `normal`；root 可传 `auditing` / `rejected` / `all`。"""

    auditStatus: Optional[str] = Field(default=None, description="过滤状态；None→normal；root 可用 auditing/rejected/all")
    page: int = Field(default=1, ge=1, description="页码")
    perPage: int = Field(default=20, ge=1, le=200, description="每页条数")


class RpaTagListByTargetReq(SQLModel):
    """查询某资源关联的标签请求（仅返回 `normal`）。"""

    targetType: str = Field(description="目标资源类型")
    targetId: str = Field(description="目标资源 id（字符串）")


class RpaTagItemResp(SQLModel):
    """标签条目（供前端渲染）。pubTime / createdAt 序列化为 ISO 字符串或 None。"""

    id: int = Field(description="标签 id")
    name: str = Field(description="标签名称")
    color: str = Field(default="#409EFF", description="标签颜色")
    createdBy: Optional[int] = Field(default=None, description="创建者 mid")
    auditStatus: str = Field(description="审核状态：auditing / normal / rejected")
    pubTime: Optional[str] = Field(default=None, description="审核通过上架时间（ISO）")
    createdAt: Optional[str] = Field(default=None, description="创建时间（ISO）")


class RpaTagListResp(SQLModel):
    """列出标签响应。"""

    page: int = Field(description="页码")
    perPage: int = Field(description="每页条数")
    total: int = Field(description="总数")
    items: list[RpaTagItemResp] = Field(default_factory=list, description="标签列表")