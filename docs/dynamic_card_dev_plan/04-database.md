[← 返回目录](./README.md)

## 四、数据库设计（MySQL，be-message 主库 `BiliMessageDB`）

所有表均使用 **camelCase 列名**、`BIGINT` 主键、时间戳统一走 `TimestampMixin`（datetime + `default_factory` + `onupdate`），对齐现有 `app/models/db/` 下的模型规范（如 [comment.py](file:///home/minato/BilibiliExplosion/be-message-service/app/models/db/comment.py)、[base.py](file:///home/minato/BilibiliExplosion/be-message-service/app/models/db/base.py)）。

> **存储引擎说明（1.0.1 修正）**：Moment 卡片相关的全部数据表**落在 be-message 的 MySQL 主库**（`BiliMessageDB`），由 `alembic/`（alembic.ini）分支统一纳管，**不是** pptr 的 Postgres 库。
>
> - JSON 正文用 `sa.JSON()`（非 PG 的 `JSONB`）；
> - 时间戳用 `datetime` + `TimestampMixin`（`onupdate=datetime.now()`），**不使用** `DateTime(timezone=True)` / `func.now()`；
> - 枚举列用 `int_enum_type()` / `str_enum_type()` 映射为 `INTEGER` / `VARCHAR` 存 value（`native_enum=False`，新增枚举值无需 DDL），**不使用** PG 原生 ENUM；
> - `mid` 系用户字段（`mid` / `accusedMid` / `reportMid` / `operatorMid`）**仅存 BIGINT，不建跨库外键**——用户主数据在 pptr Postgres（`TUserInfo`），MySQL 主库惯例不引用，渲染时由 `PptrUserService` 只读回查（与 `msg_user_follow` / `msg_user_ban` 一致）；
> - 仅 `TMoment.repostSrcDynId → TMoment.dynId` 为同库自引用 FK（`ON DELETE SET NULL`）。

### 4.1 Moment 主表 `TMoment`

| 列名 | 类型 | 约束 | 说明 |
|---|---|---|---|
| `dynId` | BIGINT | PK, autoincrement | Moment ID（雪花 ID 生成器） |
| `mid` | BIGINT | 仅存 UID，不建跨库 FK（用户主数据在 pptr Postgres `TUserInfo`），NOT NULL | 发布者 UID |
| `dynType` | INT | NOT NULL | Moment 类型（MomentTypeEnum MVP: 1=FORWARD, 6=WORD） |
| `bizRid` | BIGINT | NULL | 业务资源 ID（MVP 保留字段，后续 AV/PGC/LIVE 迭代用） |
| `bizType` | INT | NULL | 业务资源类型（InteractionBizTypeEnum 存 int 值：1=dynamic,2=lottery,3=rpa_action,4=rpa_workflow,5=rpa_browser,6=rpa_plugin） |
| `contentText` | TEXT | NULL | 纯文本正文（便于全文搜索；从 contentJson 去标签提取） |
| `contentJson` | JSON | NOT NULL | 结构化正文内容（富文本节点；支持外链图片 URL 节点 LINK 类型） |
| `repostSrcDynId` | BIGINT | FK → `TMoment.dynId`, NULL | 转发源Moment ID（转发链；FORWARD 类型必填） |
| `repostDepth` | INT | DEFAULT 0 | 转发嵌套深度（超过 N 层截断显示） |
| `topicId` | BIGINT | NULL | 主话题 ID（2.22.0 起 = topics[0]；兼容存量数据与话题 Feed 主查询；完整多话题见 §4.4.1 `TMomentTopicRel`） |
| `lbsPoi` | VARCHAR(255) | NULL | LBS 位置 POI |
| `lbsLat` | DOUBLE | NULL | 纬度 |
| `lbsLng` | DOUBLE | NULL | 经度 |
| `visibleScope` | INT | DEFAULT 0 | 可见范围：0=公开, 1=仅关注, 2=仅自己, 3=充电专享 |
| `closeComment` | INT | DEFAULT 0 | 是否关闭评论：0=否, 1=是 |
| `upChooseComment` | INT | DEFAULT 0 | UP 精选评论开关 |
| `foldType` | INT | DEFAULT 0 | 折叠类型：0=无, 1=用户折叠, 2=超频折叠 |
| `auditStatus` | VARCHAR(16) | DEFAULT 'auditing' | 审核状态：auditing/normal/rejected/hidden — 发布默认为审核中 |
| `auditRejectReason` | VARCHAR(500) | NULL | 最近一次驳回原因（前端显示在 rejected 状态卡片上） |
| `isTop` | INT | DEFAULT 0 | 是否空间置顶：0=否, 1=是（仅 normal 状态可设） |
| `topTime` | TIMESTAMPTZ | NULL | 置顶时间 |
| `timerPubTime` | TIMESTAMPTZ | NULL | 定时发布时间（NULL=立即进入审核队列） |
| `pubTime` | TIMESTAMPTZ | NULL | 实际对外发布时间（审核通过时写入 now()，未通过=NULL）；Feed 排序基于此字段 |
| `createdAt` | TIMESTAMPTZ | DEFAULT now() | |
| `updatedAt` | TIMESTAMPTZ | DEFAULT now(), onupdate=now() | |
| `deletedAt` | TIMESTAMPTZ | NULL | 软删时间（不为 NULL 时所有对外接口视为不存在） |

> **审核状态流转规则**：
> - 新建 → `auditStatus='auditing'`，`pubTime=NULL`，仅作者空间可见。
> - 管理员通过 → `auditStatus='normal'`，`pubTime=now()`，进入 Feed 流，**不通知作者**。
> - 管理员驳回 → `auditStatus='rejected'`，保留 `pubTime=NULL`，写 `auditRejectReason`，**发驳回通知给作者**。
> - 作者编辑 rejected/auditing Moment → 重置 `auditStatus='auditing'`，清空 `auditRejectReason`。
> - 软删 → 设置 `deletedAt=now()`，同时 `isTop=0`（取消置顶），所有对外接口过滤掉。

**索引：**

- `idx_dynamic_mid_pubtime (mid, pubTime DESC)` → 空间页 Feed（WHERE deletedAt IS NULL AND auditStatus='normal'）
- `idx_dynamic_mid_created (mid, createdAt DESC)` → 作者本人空间视图（含所有状态）
- `idx_dynamic_auditing_created (auditStatus, createdAt DESC)` → 管理员审核队列（auditStatus='auditing' 倒序）
- `idx_dynamic_topic_pubtime (topicId, pubTime DESC)` → 话题 Feed
- `idx_dynamic_pubtime_visible (pubTime DESC, visibleScope)` → 综合页 Feed
- `idx_dynamic_repost_src (repostSrcDynId, auditStatus)` → repostCount 补偿对账脚本用（按源 dynId + 状态快速扫描）
- `idx_dynamic_biz (bizType, bizRid)` → 按资源反查Moment（后续迭代用）

### 4.2 Moment 统计表 `TMomentStat`

| 列名 | 类型 | 约束 | 说明 |
|---|---|---|---|
| `dynId` | BIGINT | PK, FK → `TMoment.dynId` | 1:1 关联 |
| `likeCount` | BIGINT | DEFAULT 0 | 点赞数（**明细表 + 计数原子增减范式**：INSERT/DELETE `TMomentLike` 后同事务原子 UPDATE） |
| `commentCount` | BIGINT | DEFAULT 0 | 评论数（由评论系统写评论明细后回调同事务 UPDATE） |
| `repostCount` | BIGINT | DEFAULT 0 | 转发数（**原子 UPDATE 数字字段，状态机驱动 ±1**） — 详见下方「计数双写范式」 |
| `viewCount` | BIGINT | DEFAULT 0 | 浏览数（TMomentViewLog upsert 首次 0→1 时原子 +1） |
| `shareCount` | BIGINT | DEFAULT 0 | 分享数（预留；按明细表 + 计数范式实现） |
| `coinCount` | BIGINT | DEFAULT 0 | 投币数（预留） |
| `favoriteCount` | BIGINT | DEFAULT 0 | 收藏数（预留） |
| `updatedAt` | TIMESTAMPTZ | DEFAULT now(), onupdate=now() | |

> **统一计数双写范式（对 likeCount / repostCount / viewCount / commentCount / shareCount 都适用）**：
>
> 1. **明细表（唯一约束做幂等）**：先写/删明细表（`TMomentLike` / `TMomentViewLog` / 评论明细），利用 DB 唯一约束保证"同一行为在同一维度上只发生 1 次语义"。
> 2. **计数原子增减**：明细操作成功**且产生实际语义变化**（新增语义 → +1；撤销语义 → -1）时，紧接着在**同一个数据库事务**里对 `TMomentStat.<col>` 做 `UPDATE ... SET col = col + delta WHERE dynId = ?`，delta 只能是 ±1，完全依赖 PG 原子 UPDATE 防并发丢失。
> 3. **幂等保护**：明细唯一约束冲突时，跳过计数 UPDATE，整个接口仍然返回成功（而不是报错），因此「用户重试 → 计数不被意外叠加」。
> 4. **补偿对账（非热路径）**：仅在夜间定时或人工运维脚本里做 `COUNT(*) FROM 明细 WHERE 条件` 与 `TMomentStat` 字段比较，发现不一致则 UPDATE 对齐一次。**请求链路（Feed/详情等）严禁触发 COUNT 聚合查询**。

> **repostCount 状态机触发点（±1 的唯一合法修改点）**：
>
> repostCount 只统计"当前 auditStatus=normal 且 deletedAt IS NULL 的 FORWARD 类型子Moment数量"。任何会导致 FORWARD Moment跨越 counting/non-counting 边界的动作，都必须同时修改 `srcDyn.repostCount`：
>
> | # | 动作 | FORWARD Moment before | FORWARD Moment after | 是否计数变化 | 对 srcDyn.repostCount 的修改 |
> |---|---|---|---|---|---|
> | ① | 管理员审核通过（P6-T2 approve 接口） | auditing | normal | 进入 counting | +1 |
> | ② | 管理员审核驳回（P6-T3 reject 接口） | normal | rejected | 离开 counting | -1 |
> | ③ | 用户编辑自己的 normal 转发Moment（P2-T4 edit 服务） | normal | auditing（重新审核） | 离开 counting | -1；若后续再通过则走①再 +1 |
> | ④ | 用户删除自己的 normal 转发Moment（P2-T4 remove 服务软删） | normal | deletedAt ≠ NULL | 离开 counting | -1 |
> | ⑤ | 用户删除 auditing/rejected 转发Moment | 不在 counting | 离开（仍不 counting） | 无变化 | 不改 |
> | ⑥ | 新建 FORWARD 转发时 | - | auditing（默认） | 还没进 counting | 不改（此时**不要** +1，必须等审核通过后才加） |
>
> 注意：repostCount 增减的操作对象永远是 `repostSrcDynId` 指向的**源Moment**那一行的 `TMomentStat.repostCount`，不是转发Moment自己的。

> **likeCount 双写链路（点赞幂等）**：
>
> 点赞接口（P4-T3）事务步骤：
> 1. 查Moment `auditStatus='normal' AND deletedAt IS NULL`，否则报错拒绝。
> 2. `INSERT INTO "TMomentLike" (dynId, mid, likeType, createdAt) VALUES (?, ?, 1, now())`；捕获 `UniqueViolation`：
>    - 若用户意图是 `up=1`（点赞）且冲突 → 视为重复点赞 → 直接返回成功（不做 +1，也不报错）。
>    - 若用户意图是 `up=2`（取消）且冲突不存在 → 返回成功（本来就没点赞）。
> 3. 若 INSERT 成功且是点赞意图 → 同事务里 `UPDATE "TMomentStat" SET "likeCount" = "likeCount" + 1 WHERE "dynId" = ?`。
> 4. 若用户意图是取消点赞且明细 DELETE 成功（DELETE RETURNING 返回 1 行）→ 同事务里 `UPDATE "TMomentStat" SET "likeCount" = "likeCount" - 1 WHERE "dynId" = ? AND "likeCount" > 0`（加 `>0` 防极端负数兜底）。
> 5. 事件提醒（P6-T6）在事务提交后 fire-and-forget 生成 LIKE 事件。
>
> `viewCount`（浏览）双写链路与 likeCount 同构，唯一维度由 `UniqueConstraint(dynId, mid, refDate)` 提供，首次 upsert 产生新行才 +1。

### 4.3 Moment 点赞记录表 `TMomentLike`

| 列名 | 类型 | 约束 | 说明 |
|---|---|---|---|
| `pk` | BIGINT | PK, autoincrement | |
| `dynId` | BIGINT | FK → `TMoment.dynId`, NOT NULL | |
| `mid` | BIGINT | 仅存 UID，不建跨库 FK，NOT NULL | 点赞者 |
| `likeType` | INT | DEFAULT 1 | 点赞类型：1=普通点赞（预留扩展） |
| `createdAt` | TIMESTAMPTZ | DEFAULT now() | |

**约束：** `UniqueConstraint(dynId, mid)` → 一人一赞，**幂等双写的关键**（重复点赞 INSERT 冲突跳过计数）
**索引：** `idx_like_mid_time (mid, createdAt DESC)` → "我赞过的"列表

### 4.4 Moment 话题表 `TMomentTopic`

| 列名 | 类型 | 约束 | 说明 |
|---|---|---|---|
| `topicId` | BIGINT | PK, **非自增**（应用层雪花 ID） | 对外发布 ID 统一雪花 ID（见规则 snowflake-id.mdc），由 `generate_topic_id()` 生成 |
| `topicName` | VARCHAR(100) | UNIQUE, NOT NULL | 话题名称（唯一，创建时校验） |
| `topicCover` | VARCHAR(1024) | NULL | 话题封面图 |
| `topicDesc` | TEXT | NULL | 话题描述 |
| `jumpUrl` | VARCHAR(1024) | NULL | 话题跳转 H5 |
| `dynCount` | BIGINT | DEFAULT 0 | 话题下Moment数（冗余计数，仅统计 auditStatus='normal' 且未软删） |
| `viewCount` | BIGINT | DEFAULT 0 | 话题浏览量 |
| `isHot` | INT | DEFAULT 0 | 是否热门话题 |
| `sortWeight` | INT | DEFAULT 0 | 广场排序权重 |
| `creatorMid` | BIGINT | 仅存 UID，不建跨库 FK，NOT NULL | 创建者 UID（用户创建话题时写入；seed 灌入的真实话题置 0 表示系统/预置） |
| `auditStatus` | VARCHAR(16) | DEFAULT 'auditing' | 审核状态：auditing/normal/rejected — 用户创建默认审核中，不公开展示 |
| `auditRejectReason` | VARCHAR(500) | NULL | 最近一次驳回原因（前端显示在「我的话题」rejected 状态卡片上） |
| `pubTime` | TIMESTAMPTZ | NULL | 实际对外发布时间（审核通过时写入 now()，未通过=NULL）；广场/热搜排序基于此字段 |
| `createdAt` | TIMESTAMPTZ | DEFAULT now() | |
| `updatedAt` | TIMESTAMPTZ | DEFAULT now(), onupdate=now() | |

> **话题审核状态流转规则（2.19.0 起，对齐动态审核）**：
> - 用户创建 → `auditStatus='auditing'`，`pubTime=NULL`，不公开展示（仅创建者「我的话题」可见）。
> - 管理员通过 → `auditStatus='normal'`，`pubTime=now()`，进入话题广场/Feed/热搜，**不通知创建者**。
> - 管理员驳回 → `auditStatus='rejected'`，保留 `pubTime=NULL`，写 `auditRejectReason`，**发驳回通知给创建者**。
> - seed 灌入的真实话题：`auditStatus='normal'`、`pubTime=now()`、`creatorMid=0`（预置/系统话题，无需审核）。
> - 对外可见过滤：话题广场 `/topic/square`、话题 Feed `/topic/{topicId}/feed`、热搜 `/topic/hot-search` 均只展示 `auditStatus='normal'` 的话题；发布动态关联话题时校验目标话题 `auditStatus='normal'`（否则 422 拒绝）。

**索引（2.19.0 起新增）：**
- `idx_topic_audit_created (auditStatus, createdAt DESC)` → 管理端话题审核队列（auditStatus='auditing' 倒序）
- `idx_topic_creator_created (creatorMid, createdAt DESC)` → 「我创建的话题」（含全部状态）

> **雪花 ID（对外发布 ID 统一规则，见 `.codebuddy/rules/snowflake-id.mdc`）**：
> `topicId` 为**非自增**雪花 ID（位布局：31 bits 分钟时间戳 + 4 bits worker + 4 bits 序列号 = 39 bits），
> 由 `bili-common/bili_common/core/snowflake.py::MinuteSnowflakeIdGenerator` 生成，be-message-service 侧
> `app/core/sharding.py::generate_topic_id()` 封装（独立配置 `topic_id_worker_id` / `topic_id_epoch_sec`，
> 与 uid / moment_id 数值空间分离）。创建话题时由应用层显式赋值 `topicId`，数据库不做自增。

### 4.4.1 动态-话题多对多关系表 `TMomentTopicRel`（2.22.0 新增）

一条动态允许关联多个话题（上限 `_MAX_TOPIC_COUNT`=5，去重）。对标 B 站 `ModuleExtend { repeated ModuleExtendItem extend }` / `EditDynRsp.topic_infos`（repeated）。**不冗余存话题名称快照**，名称只读 `TMomentTopic` 回填（话题改名全局生效）；`TMoment.topicId` 保留为主话题（= topics[0]），保证存量单话题数据与话题 Feed 主查询（`idx_dynamic_topic_pubtime`）兼容。

| 列名 | 类型 | 约束 | 说明 |
|---|---|---|---|
| `pk` | BIGINT | PK, autoincrement | 内部物理主键，不对外暴露 |
| `dynId` | BIGINT | FK → `TMoment.dynId`（ON DELETE CASCADE）, NOT NULL | 动态 ID |
| `topicId` | BIGINT | NOT NULL | 话题 ID（仅存 ID，话题主数据在 `TMomentTopic`，不建 FK 以规避循环引用成本） |
| `createdAt` | TIMESTAMPTZ | DEFAULT now() | |

**约束：** `UniqueConstraint(dynId, topicId)` → 同一条动态同话题只关联一次（发布/编辑时先去重再写入）。

**索引：**
- `idx_topic_rel_topic (topicId, dynId)` → 话题 Feed（`WHERE topicId = ?` JOIN `TMoment` 按 pubTime 倒序分页）

**读写约定（2.22.0）：**
- 发布/编辑：`topics`（与兼容 `topic` 合并去重）逐一校验存在且 `auditStatus='normal'`（否则 422）后，主话题写 `TMoment.topicId`，全部话题写本表；编辑时先 `DELETE` 旧关系再批量 `INSERT`（同一事务）。
- 存量数据（仅 `TMoment.topicId` 有值、本表无行）：读时兼容——Feed/详情 extend 模块的 `topics` 以「本表行 + `topicId` 主话题」并集为准，`topic_feed` 查询 `TMoment.topicId = ? OR dynId IN (本表)` 双条件兜底。
- 话题删除（软删）：不级联删除本表行，装配回填时以 `TMomentTopic` 实际存在与否过滤。

### 4.5 Moment 浏览去重表 `TMomentViewLog`

| 列名 | 类型 | 约束 | 说明 |
|---|---|---|---|
| `pk` | BIGINT | PK, autoincrement | |
| `dynId` | BIGINT | NOT NULL | |
| `mid` | BIGINT | NOT NULL | |
| `refDate` | VARCHAR(10) | NOT NULL | 日期 YYYY-MM-DD |
| `viewCount` | INT | DEFAULT 1 | 当日浏览次数 |
| `lastViewAt` | TIMESTAMPTZ | DEFAULT now() | |

**约束：** `UniqueConstraint(dynId, mid, refDate)` → 同一用户同一天只计一次浏览量
**策略：** 先 upsert 此表，只有首次插入（`xmax = 0`）才给 `TMomentStat.viewCount` +1；后续同日重复调用只自增 `TMomentViewLog.viewCount`，不累加 `TMomentStat.viewCount`。

### 4.6 Moment 举报表 `TMomentReport`

| 列名 | 类型 | 约束 | 说明 |
|---|---|---|---|
| `pk` | BIGINT | PK, autoincrement | |
| `dynId` | BIGINT | FK → `TMoment.dynId`, NOT NULL | 被举报Moment |
| `accusedMid` | BIGINT | 仅存 UID，不建跨库 FK，NOT NULL | 被举报用户（= TMoment.mid，冗余便于查询） |
| `reportMid` | BIGINT | 仅存 UID，不建跨库 FK，NOT NULL | 举报人 |
| `reasonType` | INT | NOT NULL | 举报原因类型（ReportReasonEnum 枚举） |
| `reasonDesc` | TEXT | NULL | 补充描述（选填） |
| `auditStatus` | VARCHAR(16) | DEFAULT 'pending' | pending/resolved/rejected |
| `auditRemark` | VARCHAR(500) | NULL | 审核处理备注 |
| `auditAdminMid` | BIGINT | NULL | 处理管理员 MID |
| `createdAt` | TIMESTAMPTZ | DEFAULT now() | |

### 4.7 Moment 审核记录表 `TMomentAuditLog`

> 记录每一次审核流转：发布、编辑、管理员通过/驳回。用于管理员后台流水和审计。

| 列名 | 类型 | 约束 | 说明 |
|---|---|---|---|
| `pk` | BIGINT | PK, autoincrement | |
| `dynId` | BIGINT | FK → `TMoment.dynId`, NOT NULL | 被审核的Moment |
| `operatorMid` | BIGINT | 仅存 UID，不建跨库 FK，NOT NULL | 操作人 MID（作者=发布/编辑；管理员=通过/驳回） |
| `operatorRole` | VARCHAR(16) | NOT NULL | `author` / `admin`（区分作者本人操作还是管理员操作） |
| `fromStatus` | VARCHAR(16) | NULL | 流转前 auditStatus |
| `toStatus` | VARCHAR(16) | NOT NULL | 流转后 auditStatus |
| `actionType` | VARCHAR(16) | NOT NULL | `create`/`edit`/`approve`/`reject`/`resubmit`/`delete` |
| `rejectReason` | VARCHAR(500) | NULL | 驳回原因（仅 actionType=reject 有值） |
| `remark` | VARCHAR(500) | NULL | 其他备注 |
| `clientIp` | VARCHAR(64) | NULL | 操作者 IP |
| `userAgent` | VARCHAR(512) | NULL | 操作者 UA |
| `createdAt` | TIMESTAMPTZ | DEFAULT now() | |

**索引：**
- `idx_audit_log_dynid_created (dynId, createdAt DESC)` → 单条Moment 审核历史
- `idx_audit_log_admin_created (operatorMid, createdAt DESC)` → 管理员审核历史
- `idx_audit_log_action_created (actionType, createdAt DESC)` → 按 action 过滤

### 4.8 收藏明细表 `TMomentFavorite`（2.17.0 泛化）

> 从「仅收藏动态」泛化为「收藏任意业务资源」。`bizType` + `bizId` 唯一定位一个可收藏资源；动态资源（`bizType='dynamic'`）时 `bizId=dynId` 并保留 `dynId` 冗余列以兼容既有查询。

| 列名 | 类型 | 约束 | 说明 |
|---|---|---|---|
| `pk` | BIGINT | PK, autoincrement | |
| `bizType` | INT | NOT NULL, DEFAULT 1 | 资源类型（InteractionBizTypeEnum 存 int：1=dynamic,2=lottery,3=rpa_action,4=rpa_workflow,5=rpa_browser） |
| `bizId` | BIGINT | NOT NULL | 资源 ID（动态时 = dynId） |
| `dynId` | BIGINT | NULL | 冗余兼容列（bizType=1(dynamic) 时与 bizId 相同；非动态为 NULL） |
| `folderId` | BIGINT | NOT NULL | 所属收藏夹 id |
| `mid` | BIGINT | 仅存 UID，不建跨库 FK，NOT NULL | 收藏用户 |
| `note` | VARCHAR(200) | NULL | 收藏备注（预留） |
| `createdAt` | TIMESTAMPTZ | DEFAULT now() | |

**约束：** `UniqueConstraint(bizType, bizId, folderId)` → 同一收藏夹对同一资源不重复收藏（替代原 `(dynId, folderId)`）
**索引：** `idx_fav_mid_created (mid, createdAt DESC)`（我收藏的列表）、`idx_fav_folder_created (folderId, createdAt DESC)`、`idx_fav_biz (bizType, bizId)`

> **计数双写**：收藏/取消收藏时，动态资源（`bizType='dynamic'`）原子 ±1 `TMomentStat.favoriteCount`（按用户去重，即同一用户在所有收藏夹都取消后才 -1）；非动态资源原子 ±1 `TInteractionStat.favoriteCount`。

### 4.9 点赞明细表 `TMomentLike`（2.17.0 泛化）

| 列名 | 类型 | 约束 | 说明 |
|---|---|---|---|
| `pk` | BIGINT | PK, autoincrement | |
| `bizType` | INT | NOT NULL, DEFAULT 1 | 资源类型（InteractionBizTypeEnum 存 int：1=dynamic,2=lottery,...） |
| `bizId` | BIGINT | NOT NULL | 资源 ID（动态时 = dynId） |
| `dynId` | BIGINT | NULL | 冗余兼容列（动态时与 bizId 相同） |
| `mid` | BIGINT | 仅存 UID，不建跨库 FK，NOT NULL | 点赞者 |
| `likeType` | INT | DEFAULT 1 | 点赞类型：1=普通点赞（预留扩展） |
| `createdAt` | TIMESTAMPTZ | DEFAULT now() | |

**约束：** `UniqueConstraint(bizType, bizId, mid)` → 一人一赞，幂等双写的关键
**索引：** `idx_like_mid_time (mid, createdAt DESC)`（我赞过的列表）、`idx_like_biz (bizType, bizId)`

> **计数双写**：动态资源 `TMomentStat.likeCount` 原子 ±1；非动态资源 `TInteractionStat.likeCount` 原子 ±1。

### 4.10 通用交互计数表 `TInteractionStat`（2.17.0 新增）

> 仅承载**非动态资源**（lottery / rpa_*）的交互计数；动态资源计数仍走 `TMomentStat`。统一「明细表幂等 + 计数原子 ±1」范式。

| 列名 | 类型 | 约束 | 说明 |
|---|---|---|---|
| `bizType` | INT | PK（联合） | 资源类型（InteractionBizTypeEnum 存 int：2=lottery,3=rpa_action,4=rpa_workflow,5=rpa_browser,6=rpa_plugin） |
| `bizId` | BIGINT | PK（联合） | 资源 ID |
| `likeCount` | BIGINT | DEFAULT 0 | 点赞数（TMomentLike 明细 + 原子 ±1） |
| `favoriteCount` | BIGINT | DEFAULT 0 | 收藏数（TMomentFavorite 明细 + 原子 ±1） |
| `viewCount` | BIGINT | DEFAULT 0 | 浏览数（TInteractionViewLog 去重 + 首次原子 +1；2.23.0） |
| `createdAt` | TIMESTAMPTZ | DEFAULT now() | |
| `updatedAt` | TIMESTAMPTZ | DEFAULT now(), onupdate=now() | |

**约束：** `PrimaryKeyConstraint(bizType, bizId)` → 每种资源一行计数

> **2.18.0 通用收口**：`TInteractionStat` 的字段定义与 `InteractionStatService`（原子 ±1 / 批量读）统一收口到 bili-common
> （`InteractionStatBase` 基类 + `InteractionStatService` 通用服务），be-message 的 `TInteractionStat` 继承 `InteractionStatBase`
> 建立物理表，`BeMessageInteractionStatService` 绑定该表。`bizType` 取值含 `lottery` / `rpa_action` / `rpa_workflow` /
> `rpa_browser` / `rpa_plugin`（`InteractionBizTypeEnum`，bili-common）。资源实体详情不存本表，按需经 RPC 从资源归属服务获取（见 §5.3 跨服务资源详情）。

### 4.10.1 通用浏览去重表 `TInteractionViewLog`（2.23.0 新增）

> 非动态资源（lottery / rpa_\*）的浏览去重明细，完全对标 `TMomentViewLog`（§4.5）的
> 「明细表唯一约束幂等 + 计数原子 ±1」范式；动态资源浏览仍走 `TMomentViewLog`。

| 列名 | 类型 | 约束 | 说明 |
|---|---|---|---|
| `pk` | BIGINT | PK, autoincrement | |
| `bizType` | INT | NOT NULL | 资源类型（InteractionBizTypeEnum 存 int：2=lottery,...） |
| `bizId` | BIGINT | NOT NULL | 资源 id（雪花 id） |
| `mid` | BIGINT | NOT NULL | 浏览用户 mid |
| `refDate` | VARCHAR(10) | NOT NULL | 统计日期（YYYY-MM-DD） |
| `viewCount` | INT | DEFAULT 1 | 当日该用户累计浏览次数（重复访问只累加本列） |
| `lastViewAt` | TIMESTAMPTZ | DEFAULT now() | 最近浏览时间 |
| `createdAt` | TIMESTAMPTZ | DEFAULT now() | |

**约束：** `UniqueConstraint(bizType, bizId, mid, refDate)` → 同一用户同一资源同一天只计一次 Stat 浏览量
**策略：** 先 upsert 本表，仅首次插入才给 `TInteractionStat.viewCount` +1；同日重复只自增 `TInteractionViewLog.viewCount`，不累加 Stat。

### 4.11 动态正文 RESOURCE 节点（2.17.0 新增）

`contentJson` 富文本节点新增通用 `RESOURCE` 类型，用于在动态正文中引用抽奖卡片 / RPA 操作等任意业务资源：

```json
{ "type": "RESOURCE", "bizType": "lottery", "bizId": "123", "name": "抽奖卡片标题", "cover": "https://.../cover.jpg", "jumpUrl": "/app/lot-data/card-detail?id=123" }
```

前端按 `bizType` 映射到对应落地页渲染跳转；服务端仅存储与校验 `bizType`/`bizId` 非空，不做跨服务数据回查。

> **attach 卡片（转发抽奖）不存快照，读取时 RPC 批量实时获取（2.20.1）**：动态引用抽奖卡片（`RESOURCE{bizType=lottery}`）时，`contentJson` **只落库 `bizType`/`bizId`/`name`（卡片信息不落库）**。读取动态（Feed/详情装配管线 `_build_feed_item`）时，对 RESOURCE=lottery 节点经 RPC 从 be-bilibili-crawler 实时拉取 lottery 详情填充 `name`/`cover`/`jumpUrl` 后返回前端；**Feed 流先收集本页全部动态去重后的 lottery_id 批量回查一次**，再按动态分发填充，避免逐条 N+1：
> - `name`：抽奖标题（`Lotdata.first_prize_cmt`）
> - `cover`：封面图链接（`Lotdata.first_prize_pic`，无则空）
> - `jumpUrl`：跳转链接（`Lotdata.lottery_detail_url`，缺省降级为空）
>
> 数据库不保存卡片快照，始终以源资源为准；RPC 失败/源删除时弱依赖降级（保留原节点或返回空信息），不影响动态正文渲染。**注意**：`bizType=lottery` 的 RESOURCE 节点在 create 时经 `InteractionResourceValidator` RPC 校验源资源存在（不存在 422 拒绝）。

---

> **MVP 未包含的表（后续迭代补建）**：
> - `TMomentMedia` — 图片上传/存储；当前仅支持正文中外链图片 URL，不做上传。
> - `TMomentAttachCard` — 投票/预约/商品等附加大卡配置；当前 MVP 不提供附加卡。
> - 其他 AV/PGC/ARTICLE/LIVE 等业务资源表已在其他模块复用，此处不重复建设。
