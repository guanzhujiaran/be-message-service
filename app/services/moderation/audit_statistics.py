"""通用审核统计共享聚合（计划书 §5.13）。

各业务域的 `statistics()` 静态方法落在各自的 Biz / Service 类上
（MomentAuditService / MomentTopicAuditService / CommentAdminService /
DmAdminService / AvatarAuditService / FolderCoverAuditService / ReportService），
本模块只提供它们共用的聚合-建模 helper（无业务类依赖，避免循环导入）：

- `_status_key` / `_type_key`：枚举 / 原始值 → 统一小写状态键、类型名；
- `agg_rows_to_resp`：把 ``[(type_name|None, status_name, count)]`` 聚合行
  一次建模为 `AuditStatisticsResp`（byType 行只填该域涉及的状态列，其余列
  保持 None，由路由 `response_model_exclude_none=True` 剔除出响应）。

约定：各域统计全部一次 GROUP BY 完成，禁止循环发 COUNT；
私信按 msgkey 去重（写扩散双行状态一致）。
"""

from app.models.schemas.audit import AuditStatisticsResp, AuditTypeCountRow


def status_key(v) -> str:
    """枚举 / 原始值 → 统一小写状态键（auditing/normal/…、pending/…）。"""
    return (v.name if hasattr(v, "name") else str(v)).lower()


def type_key(v) -> str:
    """枚举 / 原始值 → 子类型名（MomentTypeEnum / InteractionBizTypeEnum 成员名）。"""
    return v.name if hasattr(v, "name") else str(v)


def agg_rows_to_resp(rows) -> AuditStatisticsResp:
    """聚合行 → 类型化统计响应。

    rows: 可迭代的 ``(type_name | None, status_name, count)``；
    type 为 None 的行只计入 byStatus / total。全部行都无 type 时
    （话题 / 头像 / 封面 / 私信），byType 置 None——响应中省略，只有 status 维度。
    """
    by_status: dict[str, int] = {}
    buckets: dict[str, AuditTypeCountRow] = {}
    total = 0
    for tname, sname, cnt in rows:
        by_status[sname] = by_status.get(sname, 0) + cnt
        total += cnt
        if tname is not None:
            row = buckets.setdefault(tname, AuditTypeCountRow(type=tname))
            setattr(row, sname, (getattr(row, sname, None) or 0) + cnt)
            row.total += cnt
    return AuditStatisticsResp(
        total=total, byStatus=by_status, byType=list(buckets.values()) or None
    )
