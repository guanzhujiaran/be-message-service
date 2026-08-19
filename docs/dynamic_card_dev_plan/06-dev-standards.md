[← 返回目录](./README.md)

## 六、开发规范与代码质量标准

> 对齐现有 be-message 规范（MySQL 主库），参考 [app/models/db/base.py](file:///home/minato/BilibiliExplosion/be-message-service/app/models/db/base.py) & [app/models/db/comment.py](file:///home/minato/BilibiliExplosion/be-message-service/app/models/db/comment.py) & [enums.py](file:///home/minato/BilibiliExplosion/be-message-service/app/models/enums.py)

### 6.1 ORM 模型规范

- **必须** 使用 `SQLModel` 定义表，列名（`name`）使用 camelCase，Python 属性名与数据库列名完全一致
- **必须** 时间戳统一继承 `TimestampMixin`（`datetime` + `default_factory=datetime.now` + `onupdate=datetime.now()`），**禁止**使用 `DateTime(timezone=True)` / `func.now()`
- **必须** 使用 `Field` 参数声明 `nullable`/`primary_key`/`max_length`，`sa_type` 仅用于 `BIGINT`/`JSON`/`int_enum_type()`/`str_enum_type()` 等特殊类型
- `sa_column_kwargs` **仅**用于：`server_default`、`onupdate`、`comment`、`autoincrement`
- **禁止** 使用 `sa_column=Column(...)` 方式
- 所有外键**必须**显式声明 `ForeignKeyConstraint`；**跨库用户字段（`mid`/`accusedMid`/`reportMid`/`operatorMid）仅存 BIGINT，不建外键**（用户主数据在 pptr Postgres，渲染时只读回查）；同库自引用（如 `repostSrcDynId → dynId`）用 `SET NULL`
- 索引**必须**在 `__table_args__` 中显式声明（含命名）；DESC 排序列用 `text('"col" DESC')` 表达式
- 所有表名使用 `"T"` 前缀的 PascalCase（如 `"TMoment"`、`"TMomentStat"`）

### 6.2 枚举规范

- 数据库存储 **int** 的枚举使用 `IntEnum`（如 `MomentTypeEnum`），列类型用 `int_enum_type(EnumCls)` 映射 `INTEGER` 存 value
- 数据库存储 **VARCHAR** 的枚举使用 `StrEnum`（如 `AuditStatusEnum`），列类型用 `str_enum_type(EnumCls)` 映射 `VARCHAR` 存 value
- 对外接口响应**必须**将 int 值转换为 string 名称（如 `dynType: 6 → "WORD"`）
- 新增枚举**必须**追加到 `__all__` 并在 `app/models/db/__init__.py` / `app/models/__init__.py` 中导出

### 6.3 Service 层规范

- **禁止**在 service 内部吞异常（try-catch 只做包装 + re-raise，不静默失败），错误由统一异常中间件处理
- 跨表写入**必须**使用 `async with session.begin()` 事务，失败自动回滚
- 涉及父子链（如 `TMoment → TMomentStat`）的插入，在 `s.add(parent)` 后**必须** `await s.flush()` 再添加子行，避免 `ForeignKeyViolationError`
- ID 生成：`dynId` 使用现有 `UidGenerator`（[sharding.py](file:///home/minato/BilibiliExplosion/be-message-service/app/core/sharding.py)），注意 epoch 单位转换正确（minute-step）
- **计数双写**：明细表操作与计数 UPDATE **必须在同一个事务内**，严禁跨事务分两步写

### 6.4 API 层规范

- **必须** 使用 SQLModel 定义 Request/Response Schema（`app/models/schemas/moment.py`），**禁止**裸 dict 返回
- 分页参数**必须**有合理上限（单页最多 50 条）
- 所有写接口**必须**校验权限（仅作者本人可编辑/删除/置顶自己的Moment；审核接口仅 root/admin）
- 响应中 dynId 同时提供 `dynId`(int) 和 `dynIdStr`(string)，兼容前端大整数丢失精度

### 6.5 迁移 & 部署规范

- 所有 DDL 通过 Alembic `alembic/`（alembic.ini，be-message MySQL 主库）分支纳管，**禁止**手写 `CREATE TABLE`
- `app/core/migration.py` 在 lifespan 中自动 `run_alembic_pptr_upgrade()`，无需手动执行
- 删除表如需级联，使用 `op.execute('DROP TABLE IF EXISTS "TMoment" CASCADE')`，`op.drop_table()` 不支持 cascade
- 检查表存在用 `to_regclass('"TMoment"')`（带双引号，mixed-case 名称）

### 6.6 质量门禁

- 新增代码**必须**通过 `ruff` lint（或项目内配置的 lint 工具）
- Service 核心逻辑（点赞幂等、Feed 游标、发布事务、repostCount 状态机 ±1）**必须**有单元测试
- 敏感操作（删除Moment、置顶、审核通过/驳回）需要打点日志（logger + 操作人 IP/UA，参考 `TUserActInfoLog` 模式）
