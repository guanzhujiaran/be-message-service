# 动态卡片功能后端开发计划书

> **项目**：be-message-service 动态卡片模块
> **版本**：1.0.0
> **创建日期**：2026-08-10
> **定稿日期**：2026-08-10
> **状态**：已定稿（Signed-off），待启动开发

---

## 版本控制信息

### 版本号定义（遵循 Semantic Versioning 2.0.0）

版本号格式：`MAJOR.MINOR.PATCH`（X.Y.Z，X/Y/Z 均为非负整数，不含前导零）

| 段位 | 当前值 | 含义（严格对齐 SemVer 2.0.0） |
|---|---|---|
| MAJOR (X) | 1 | 1.0.0 定义了本计划书的**公共 API**（即已冻结的技术决策、数据库 schema、API 接口规范、开发标准）。X > 0 后，任何对公共 API 的**backward incompatible changes**必须递增 X，同时 Y、Z 归零 |
| MINOR (Y) | 0 | 在**backward compatible**前提下，新增功能模块/数据表/API 端点/阶段任务，或对公共 API 功能标记 deprecated 时递增 Y，Z 归零 |
| PATCH (Z) | 0 | 仅引入**backward compatible bug fixes**（修正计划书中的错误行为/不一致，不改变范围与语义）时递增 Z |

> **0.y.z 阶段说明**：MAJOR 为 0 时表示初始开发阶段，任何内容 MAY 在任意时间变更，公共 API SHOULD NOT 被视为稳定。本计划书定稿前的 0.1.0 ~ 0.4.0 即属此阶段。
>
> **1.0.0 阶段说明**：1.0.0 标志公共 API 首次定义并冻结，后续版本号递增方式严格依本公共 API 的变更类型而定。
>
> **扩展支持**：必要时可附加 pre-release 标识（如 `1.0.0-alpha`、`1.0.0-rc.1`）与 build metadata（如 `1.0.0+20260810`），分别用 `-` 和 `+` 分隔；build metadata 不参与版本先后比较。

### 变更记录（Changelog）

| 版本 | 日期 | 变更类型 | 变更摘要 |
|---|---|---|---|
| 0.1.0 | 2026-08-10 | MINOR (0.y.z) | 初稿：基于 B站 Proto 反向推导数据模型，完成功能模块划分与分阶段计划（0.y.z 阶段，API 不视为稳定） |
| 0.2.0 | 2026-08-10 | MINOR (0.y.z) | 新增 backward compatible 功能：数据库设计（7 表 + 索引 + FK）、API 接口规范、开发规范章节 |
| 0.3.0 | 2026-08-10 | MINOR (0.y.z) | 调整计数策略：likeCount/repostCount/viewCount 统一为「明细表唯一约束幂等 + 计数原子 UPDATE ±1 + 夜间对账补偿」范式（0.y.z 阶段允许 backward incompatible 调整，按惯例递增 Y） |
| 0.4.0 | 2026-08-10 | MINOR (0.y.z) | 新增 backward compatible 规范细节：repostCount 状态机 6 触发点表、likeCount 双写链路 5 事务步骤 |
| **1.0.0** | **2026-08-10** | **MAJOR** | **定稿：公共 API 首次定义并冻结（数据模型/模块划分/分阶段计划/DB schema/API 规范/开发标准/技术决策记录全部基线化），进入开发执行阶段** |

### 版本演进规则（严格对齐 SemVer 2.0.0 第 8-10 条）

1. **MAJOR (X)**：当对公共 API 引入任何 **backward incompatible** 变更时 MUST 递增 X，且 Y、Z MUST 归零。适用情形举例：
   - 已冻结的数据表被删除或列语义发生不兼容变更
   - 已发布的 API 端点被移除或请求/响应字段语义不兼容变更
   - 审核流程、计数范式等核心决策被推翻重做
2. **MINOR (Y)**：当向公共 API 引入 **backward compatible** 的新功能时 MUST 递增 Y，且 Z MUST 归零；任何公共 API 功能被标记 deprecated 时 MUST 递增 Y。适用情形举例：
   - 新增数据表、新增 API 端点、新增阶段任务
   - 在不影响既有语义前提下扩展字段说明或新增索引
3. **PATCH (Z)**：当仅引入 **backward compatible bug fixes**（内部修改以修正错误行为）时 MUST 递增 Z。适用情形举例：
   - 修正计划书中的笔误、数值错误、引用错链
   - 修正与既有决策不一致的描述（不改变决策本身）
4. 一旦某个版本发布，该版本内容 MUST NOT 被修改；任何修改 MUST 以新版本发布。
5. 任何基线变更必须先更新本变更记录表，再修改对应章节，并同步通知评审参与方。

---

## 目录

1. [版本控制信息](#版本控制信息)
2. [数据模型分析总结](#一数据模型分析总结基于-b站-proto-反向推导)
3. [功能模块划分](#二功能模块划分)
4. [分阶段开发进度安排](#三分阶段开发进度安排)
5. [数据库设计](#四数据库设计postgrespptr_db-分支)
6. [API 接口规范](#五api-接口规范)
7. [开发规范与代码质量标准](#六开发规范与代码质量标准)
8. [技术方案决策记录](#七技术方案决策记录)
9. [任务完成记录表](#八任务完成记录表)
10. [评审与签署](#九评审与签署)

---

## 一、数据模型分析总结（基于 B站 Proto 反向推导）

### 1.1 数据来源

基于 `be-bilibili-crawler/Service/GrpcModule/Grpc/GrpcProto/bilibili/` 下的以下 proto 文件：

- [dynamic/common/dynamic.proto](file:///home/minato/BilibiliExplosion/be-bilibili-crawler/Service/GrpcModule/Grpc/GrpcProto/bilibili/dynamic/common/dynamic.proto) — 动态通用模型（Opus、Paragraph、CreateContent、CreateScene 等）
- [dynamic/gw/gateway.proto](file:///home/minato/BilibiliExplosion/be-bilibili-crawler/Service/GrpcModule/Grpc/GrpcProto/bilibili/dynamic/gw/gateway.proto) — 动态网关模型（DynamicItem、Module、MdlDyn* 系列）
- [dynamic/interfaces/feed/v1/api.proto](file:///home/minato/BilibiliExplosion/be-bilibili-crawler/Service/GrpcModule/Grpc/GrpcProto/bilibili/dynamic/interfaces/feed/v1/api.proto) — 动态 Feed 接口（CreateDyn、DynamicRepost、DynamicThumb、RmDyn 等）
- [app/dynamic/v2/dynamic.proto](file:///home/minato/BilibiliExplosion/be-bilibili-crawler/Service/GrpcModule/Grpc/GrpcProto/bilibili/app/dynamic/v2/dynamic.proto) — V2 动态服务（DynAll、DynDetail、DynSpace、DynThumb 等）

### 1.2 核心实体关系

```
TDynamic (动态主表)
  ├── TDynamicStat     (统计数据：点赞/评论/转发/浏览 — 明细表 + 原子 ±1 双写)
  ├── TDynamicTopic    (话题关联)
  ├── TDynamicAuditLog (审核流转记录)
  ├── TDynamicLike     (点赞明细 — 幂等 + 计数)
  ├── TDynamicViewLog  (浏览去重明细 — 幂等 + 计数)
  ├── TDynamicReport   (举报)
  ├── TCommentIndex    (评论区：复用现有评论系统，CommentTypeEnum.DYNAMIC)
  └── TEventFeed       (事件提醒：点赞/评论/@/审核驳回)
```

### 1.3 动态类型枚举（MVP 仅 WORD + FORWARD，对齐 B站 DynamicType）

| 值 | 类型 | MVP 支持 | 说明 |
|---|---|---|---|
| 1 | `FORWARD` | ✅ | 转发动态 |
| 2 | `AV` | ❌ | 稿件/视频动态（后续迭代） |
| 3 | `PGC` | ❌ | 番剧/PGC（后续迭代） |
| 6 | `WORD` | ✅ | 纯文字动态（正文富文本中可插入外站图片 URL 链接） |
| 7 | `DRAW` | ❌ | 图文动态（后续迭代；当前图片走 WORD 正文外链 URL） |
| 8 | `ARTICLE` | ❌ | 专栏动态（后续迭代） |
| 12 | `LIVE` | ❌ | 直播动态（后续迭代） |
| 16 | `APPLET` | ❌ | 小程序卡（后续迭代） |
| 18 | `LIVE_RCMD` | ❌ | 直播推荐卡（后续迭代） |

> **MVP 图片方案**：不支持图片上传到服务端；用户只能在文字动态正文的富文本节点中插入**外站图片 URL 链接**（`ParagraphType.LINK + jumpUrl=图片地址`），由前端决定渲染方式。服务端仅保存为普通 `contentJson` 富文本节点，不做下载/存储/鉴真。

### 1.4 审核流程

```
  用户发布 → auditStatus = 'auditing'（Feed/详情对普通用户不可见，仅作者本人空间可见「审核中」标签）
     │
     ├─→ 管理员审核通过 → auditStatus = 'normal' → 进入 Feed，全量可见（不发通知）
     │
     └─→ 管理员审核驳回 → auditStatus = 'rejected' → 写 TDynamicAuditLog + 发驳回通知给作者（含驳回原因）
              │
              └─→ 用户可以选择：① 修改正文后重新提交（回到 auditing） ② 删除动态
```

### 1.5 模块结构（对齐 B站 DynModuleType）

每条动态由多个模块组成，前端按模块顺序渲染：

- `module_author` → 发布人信息（头像、昵称、关注按钮、发布时间）
- `module_desc` → 描述文案（富文本/@/表情/话题/外链图片 URL）
- `module_dynamic` → 正文卡（文字/转发嵌套）
- `module_forward` → 转发嵌套（源动态卡片）
- `module_extend` → 小卡（话题/LBS 标签）
- `module_stat` → 统计（点赞/评论/转发数）
- `module_interaction` → 外露交互（点赞/评论入口）

---

## 二、功能模块划分

### 模块 1：动态发布服务 (`app/services/dynamic_publish.py`)

- [ ] 纯文字动态创建（auditStatus 默认 `auditing`，支持正文富文本外链图片 URL）
- [ ] 转发动态创建（含转发链溯源，校验源动态为 `normal` 状态；创建时不 +repostCount，等审核通过再 +1）
- [ ] 动态编辑/删除（编辑非 `normal` 状态动态 → 自动回 `auditing` 重新审核；若被删/编辑的 FORWARD 动态 before=normal，则对 srcDyn.repostCount -1）
- [ ] 空间置顶/取消置顶（仅 `normal` 状态动态可置顶）
- [ ] 发布前置校验（权限、字数、@数量；MVP 限制 dynType ∈ {WORD, FORWARD}）

### 模块 2：动态 Feed 流服务 (`app/services/dynamic_feed.py`)

- [ ] 综合页 Feed（关注 + 推荐，支持分页游标；仅 `auditStatus='normal'`）
- [ ] 个人空间 Feed（指定 UID 的动态列表；**作者本人视角**包含 auditing/rejected，**访客视角**仅 normal）
- [ ] 动态详情页（单条动态完整渲染；非作者且非 normal → 返回 404 或审核中占位卡）
- [ ] 批量动态详情（批量 dyn_id 查询；非 normal 按权限过滤）
- [ ] 更新基线 & 历史偏移（支持下拉刷新 + 上拉加载）

### 模块 3：动态互动服务 (`app/services/dynamic_interaction.py`)

- [ ] 点赞 / 取消点赞（幂等，防重复计数；仅 normal 状态动态可点赞）
- [ ] 动态举报
- [ ] 预约卡 / 投票卡等附加卡交互（后续迭代，MVP 预留接口占位）

### 模块 4：动态统计服务 (`app/services/dynamic_stat.py`)

- [ ] 统一「**明细表 + 计数原子增减**」工具函数封装（`incr_stat(dynId, field, delta)` / `decr_stat(...)`，供 like/repost/view/comment 共享；严禁 COUNT 聚合在请求热路径）
- [ ] **点赞 likeCount** 原子 ±1（由 dynamic_interaction 调用，配合 `TDynamicLike` 明细唯一约束做幂等）
- [ ] **转发 repostCount** 原子 ±1（状态机 4 个关键点调用：approve/reject/编辑 normal 回审核/软删 normal 转发；修改对象是**源动态**那一行）
- [ ] **浏览 viewCount** 原子 ±1（配合 `TDynamicViewLog` 首次 upsert +1）
- [ ] **评论 commentCount** 原子 ±1（评论系统写明细后的回调入口）
- [ ] 统计数据批量读取（`SELECT * FROM "TDynamicStat" WHERE "dynId" IN (...)`；直接读字段值，**不做 COUNT 聚合**）
- [ ] 夜间/运维对账脚本（明细 COUNT 与 Stat 不一致则修正；独立 CLI 或 APScheduler；非请求链路）
- [ ] 浏览记录去重（按 `mid + dynId + refDate`；ViewLog upsert 封装在此模块）

### 模块 5：动态话题 & 标签服务 (`app/services/dynamic_topic.py`)

- [ ] 话题广场列表
- [ ] 话题 Feed 流
- [ ] @用户推荐列表（最近联系/关注/粉丝）
- [ ] @用户搜索（按昵称模糊匹配）
- [ ] POI LBS 附近地点
- [ ] POI 关键词搜索

### 模块 6：动态数据库模型 (`app/models/dynamic_db.py`)

- [ ] 所有动态相关 ORM 模型（SQLModel）
- [ ] 枚举定义补充到 `app/models/enums.py`
- [ ] Alembic 迁移脚本（pptr Postgres 分支）

### 模块 7：动态 API 路由 (`app/api/dynamic.py`, `app/api/dynamic_feed.py`, `app/api/dynamic_audit.py`)

- [ ] Feed 流接口（综合页/空间页/话题页）
- [ ] 动态发布 CRUD 接口
- [ ] 互动接口（点赞/举报）
- [ ] 审核管理接口（管理员权限守卫）
- [ ] 话题 & @ & POI 辅助接口

### 模块 8：动态事件联动（现有模块增强）

- [ ] 点赞动态 → 事件提醒（EventTypeEnum.LIKE，SourceTypeEnum.DYNAMIC）
- [ ] 评论动态 → 事件提醒（EventTypeEnum.REPLY，SourceTypeEnum.DYNAMIC）
- [ ] @用户在动态中 → 事件提醒（EventTypeEnum.AT，SourceTypeEnum.DYNAMIC）
- [ ] **审核驳回** → 事件提醒（EventSubType=AUDIT_REJECT，含驳回原因给作者）
- [ ] 动态被转发 → 源动态不发提醒；**repostCount 由状态机关键点（审核通过/驳回/编辑回审核/软删）原子 ±1 维护**

### 模块 9：动态审核服务 (`app/services/dynamic_audit.py`)

- [ ] 管理员待审核列表（按 auditing 时间倒序，分页）
- [ ] 动态审核通过（auditStatus → normal + pubTime=now()；写 TDynamicAuditLog；**不发通知**；若 dynType=FORWARD 则 srcDyn.repostCount 原子 +1）
- [ ] 动态审核驳回（auditStatus → rejected；写 auditRejectReason + TDynamicAuditLog + 发驳回事件通知；**若 dynType=FORWARD 且 before=normal 则 srcDyn.repostCount 原子 -1**）
- [ ] 动态重新审核（用户编辑 rejected 动态后 → auditing）
- [ ] 审核记录流水查询（管理员后台）
- [ ] 管理员权限校验（复用现有角色系统，仅 root/admin 可操作）

---

## 三、分阶段开发进度安排

### Phase 1：基础骨架 & 数据库

- [ ] **P1-T1**：在 `app/models/enums.py` 中新增动态相关枚举（DynamicTypeEnum MVP 仅 WORD/FORWARD；AuditStatusEnum（auditing/normal/rejected/hidden）；VisibleScopeEnum；FoldTypeEnum；ReportReasonEnum 等）
- [ ] **P1-T2**：创建 `app/models/dynamic_db.py`，定义 ORM 模型：TDynamic、TDynamicStat、TDynamicLike、TDynamicTopic、TDynamicViewLog、TDynamicReport、TDynamicAuditLog（MVP 不含 TDynamicMedia，后续迭代补充）
- [ ] **P1-T3**：在 `app/models/__init__.py` 中导出新模型
- [ ] **P1-T4**：创建 Alembic pptr 迁移脚本（`alembic_pptr/versions/`），建表 + 索引 + FK
- [ ] **P1-T5**：验证迁移脚本可正常执行（`alembic -c alembic_pptr.ini upgrade head`）

**阶段目标**：数据库评审通过，迁移脚本执行无报错

### Phase 2：动态发布 CRUD + 审核状态流转

- [ ] **P2-T1**：创建 `app/models/schemas/dynamic.py`（Request/Response SQLModel Schema）
- [ ] **P2-T2**：实现 `app/services/dynamic_publish.py` — 纯文字动态创建（auditStatus 默认 `auditing`；校验 dynType=WORD；支持正文外链图片 URL 富文本节点）
- [ ] **P2-T3**：实现转发动态创建（校验源动态 auditStatus=normal；repostSrcDynId 引用；转发深度计算；**创建时不 +repostCount，等审核通过再 +1**）
- [ ] **P2-T4**：实现动态编辑/删除（编辑 rejected/auditing 动态 → 重置为 auditing 重新审核；软删而非硬删；**若被删/编辑的 FORWARD 动态 before=normal，则对 srcDyn.repostCount -1**）
- [ ] **P2-T5**：实现空间置顶/取消置顶（仅 `normal` 且本人可操作）
- [ ] **P2-T6**：实现发布前置校验（权限、字数上限、@数量上限；MVP 拒绝 WORD/FORWARD 以外的 dynType）
- [ ] **P2-T7**：创建 `app/api/dynamic.py` 发布类路由并注册到 main.py
- [ ] **P2-T8**：编写单元测试

**阶段目标**：可发布文字/转发动态，流转 auditing → 等待审核；支持编辑/删除/置顶

### Phase 3：Feed 流 & 详情

- [ ] **P3-T1**：实现 `app/services/dynamic_feed.py` — 综合页 Feed（仅 `auditStatus='normal'`；关注列表 + 时间倒序）
- [ ] **P3-T2**：实现个人空间 Feed（本人：全部状态，访客：仅 normal；置顶动态优先排序）
- [ ] **P3-T3**：实现动态详情页（状态过滤 + 权限判断；repostCount 直接读 TDynamicStat 字段）
- [ ] **P3-T4**：实现批量动态详情（批量 dyn_id 查询，限 20 条；状态过滤）
- [ ] **P3-T5**：实现分页游标方案（updateBaseline + historyOffset + hasMore + updateNum）
- [ ] **P3-T6**：创建 `app/api/dynamic_feed.py` Feed 路由并注册
- [ ] **P3-T7**：编写单元测试

**阶段目标**：MVP 闭环（发布 → auditing → 手动改 normal → Feed 可见 → 详情可看）

### Phase 4：互动 & 统计

- [ ] **P4-T1**：实现 `app/services/dynamic_stat.py` — 统一「明细表 + 计数原子增减」工具封装（incr_stat/decr_stat）；likeCount / repostCount / viewCount / commentCount **全部走原子 UPDATE**；批量读取直接读 TDynamicStat 字段；严禁请求热路径做 COUNT 聚合
- [ ] **P4-T2**：实现 repostCount 状态机 4 个触发点的原子 ±1（由 P2/P6 调用本模块函数；含 forward 动态 before/after 状态判断）
- [ ] **P4-T3**：实现 `app/services/dynamic_interaction.py` — 点赞/取消点赞（幂等；仅 normal；事务「INSERT/DELETE TDynamicLike」 + 「UPDATE likeCount ±1」同事务；唯一冲突跳过计数）
- [ ] **P4-T4**：实现浏览上报去重（TDynamicViewLog upsert；首次新行时才 +viewCount）
- [ ] **P4-T5**：实现动态举报（写 TDynamicReport；不改变 auditStatus）
- [ ] **P4-T6**：实现夜间/运维对账脚本（可选 CLI 或 APScheduler job；明细 COUNT 与 Stat 不一致则修正）
- [ ] **P4-T7**：在 `app/api/dynamic.py` 中新增互动类路由
- [ ] **P4-T8**：编写单元测试（点赞幂等、浏览去重、repostCount 4 触发点 ±1、举报写入、计数器负数兜底）

**阶段目标**：点赞/浏览/举报功能可用，统计数据准确；repostCount 通过状态机原子 ±1 正确显示

### Phase 5：话题 & @ & LBS

- [ ] **P5-T1**：实现 `app/services/dynamic_topic.py` — 话题广场列表
- [ ] **P5-T2**：实现话题 Feed 流
- [ ] **P5-T3**：实现 @用户推荐列表（最近联系/关注/粉丝分组）
- [ ] **P5-T4**：实现 @用户搜索（昵称模糊匹配）
- [ ] **P5-T5**：实现 POI LBS 附近地点搜索
- [ ] **P5-T6**：实现 POI 关键词搜索
- [ ] **P5-T7**：在 `app/api/dynamic.py` 中新增辅助类路由
- [ ] **P5-T8**：编写单元测试

**阶段目标**：辅助功能开发完成

### Phase 6：审核服务 & 事件联动

- [ ] **P6-T1**：实现 `app/services/dynamic_audit.py` — 管理员待审核列表（分页 + 按 auditing→时间倒序）
- [ ] **P6-T2**：实现审核通过（auditStatus→normal + pubTime=now()；写 TDynamicAuditLog；**不发通知**；**dynType=FORWARD 时 srcDyn.repostCount +1**）
- [ ] **P6-T3**：实现审核驳回（auditStatus→rejected；写 auditRejectReason + TDynamicAuditLog + **发驳回事件通知**；**dynType=FORWARD 且 before=normal 时 srcDyn.repostCount -1**）
- [ ] **P6-T4**：实现审核记录流水查询（管理员后台，按 dynId / 管理员 / 时间段过滤）
- [ ] **P6-T5**：管理员权限校验（复用 `get_current_admin_user` 或 role=root 守卫装饰器）
- [ ] **P6-T6**：点赞动态 → 事件提醒（EventTypeEnum.LIKE + SourceTypeEnum.DYNAMIC）
- [ ] **P6-T7**：评论动态 → 事件提醒（EventTypeEnum.REPLY + SourceTypeEnum.DYNAMIC；复用评论系统已有事件链路，新增 DYNAMIC SourceType 分支）
- [ ] **P6-T8**：动态正文 @用户 → 事件提醒（EventTypeEnum.AT + SourceTypeEnum.DYNAMIC；发布时解析 AT 节点批量生产）
- [ ] **P6-T9**：在 `app/api/dynamic_audit.py` 中新增管理员审核路由并注册（role 守卫）
- [ ] **P6-T10**：编写单元测试

**阶段目标**：审核流完整（auditing → normal/rejected + 驳回通知）；事件中心联动完整

### Phase 7：接口联调 & 测试

- [ ] **P7-T1**：全链路冒烟测试（发布→auditing→管理员通过→Feed 可见→点赞→评论→转发→再次通过转发→repostCount +1 校验→删除转发→-1 校验）
- [ ] **P7-T2**：审核流专项测试（驳回通知到达、编辑 rejected 后回 auditing、通过不发通知）
- [ ] **P7-T3**：边界用例测试（空内容、超长文本、@数量上限、转发源为 rejected/已删 → 拒绝；非本人操作）
- [ ] **P7-T4**：权限测试（非作者无法编辑/删除/置顶；非管理员无法调用审核接口）
- [ ] **P7-T5**：并发测试（点赞幂等原子性、浏览去重幂等性、repostCount 审核/删除并发 ±1 一致性、计数器负数兜底保护）
- [ ] **P7-T6**：性能测试（Feed 流分页、批量详情、Stat 批量 IN 查询 SQL EXPLAIN；对账脚本对大数据量 COUNT 表现验证）
- [ ] **P7-T7**：Bug 修复 & 代码 review
- [ ] **P7-T8**：接口文档更新

**阶段目标**：功能验收通过，可部署上线

### 里程碑节点

| 里程碑 | 完成标志 |
|---|---|
| M1 | Phase 1 完成：数据库评审通过，迁移脚本执行无报错 |
| M2 | Phase 3 完成：可完整发布（auditing）+ 手动改 normal → Feed + 详情浏览闭环 |
| M3 | Phase 5 完成：辅助功能（话题/@/LBS）开发完成，进入审核 & 事件阶段 |
| M4 | Phase 6 完成：审核流 + 事件联动开发完成，进入联调测试 |
| M5 | Phase 7 完成：功能验收通过，可部署上线 |

---

## 四、数据库设计（Postgres，pptr_db 分支）

所有表均使用 **camelCase 列名**、`BIGINT` 主键、`onupdate=func.now()` 时间戳，对齐现有 [pptr_db.py](file:///home/minato/BilibiliExplosion/be-message-service/app/models/pptr_db.py) 规范。

### 4.1 动态主表 `TDynamic`

| 列名 | 类型 | 约束 | 说明 |
|---|---|---|---|
| `dynId` | BIGINT | PK, autoincrement | 动态 ID（雪花 ID 生成器） |
| `mid` | BIGINT | FK → `TUserInfo.uid`, NOT NULL | 发布者 UID |
| `dynType` | INT | NOT NULL | 动态类型（DynamicTypeEnum MVP: 1=FORWARD, 6=WORD） |
| `bizRid` | BIGINT | NULL | 业务资源 ID（MVP 保留字段，后续 AV/PGC/LIVE 迭代用） |
| `bizType` | VARCHAR(32) | NULL | 业务资源类型（MVP 基本为 NULL，后续迭代用） |
| `contentText` | TEXT | NULL | 纯文本正文（便于全文搜索；从 contentJson 去标签提取） |
| `contentJson` | JSONB | NOT NULL | 结构化正文内容（富文本节点；支持外链图片 URL 节点 LINK 类型） |
| `repostSrcDynId` | BIGINT | FK → `TDynamic.dynId`, NULL | 转发源动态 ID（转发链；FORWARD 类型必填） |
| `repostDepth` | INT | DEFAULT 0 | 转发嵌套深度（超过 N 层截断显示） |
| `topicId` | BIGINT | NULL | 关联话题 ID |
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
> - 作者编辑 rejected/auditing 动态 → 重置 `auditStatus='auditing'`，清空 `auditRejectReason`。
> - 软删 → 设置 `deletedAt=now()`，同时 `isTop=0`（取消置顶），所有对外接口过滤掉。

**索引：**

- `idx_dynamic_mid_pubtime (mid, pubTime DESC)` → 空间页 Feed（WHERE deletedAt IS NULL AND auditStatus='normal'）
- `idx_dynamic_mid_created (mid, createdAt DESC)` → 作者本人空间视图（含所有状态）
- `idx_dynamic_auditing_created (auditStatus, createdAt DESC)` → 管理员审核队列（auditStatus='auditing' 倒序）
- `idx_dynamic_topic_pubtime (topicId, pubTime DESC)` → 话题 Feed
- `idx_dynamic_pubtime_visible (pubTime DESC, visibleScope)` → 综合页 Feed
- `idx_dynamic_repost_src (repostSrcDynId, auditStatus)` → repostCount 补偿对账脚本用（按源 dynId + 状态快速扫描）
- `idx_dynamic_biz (bizType, bizRid)` → 按资源反查动态（后续迭代用）

### 4.2 动态统计表 `TDynamicStat`

| 列名 | 类型 | 约束 | 说明 |
|---|---|---|---|
| `dynId` | BIGINT | PK, FK → `TDynamic.dynId` | 1:1 关联 |
| `likeCount` | BIGINT | DEFAULT 0 | 点赞数（**明细表 + 计数原子增减范式**：INSERT/DELETE `TDynamicLike` 后同事务原子 UPDATE） |
| `commentCount` | BIGINT | DEFAULT 0 | 评论数（由评论系统写评论明细后回调同事务 UPDATE） |
| `repostCount` | BIGINT | DEFAULT 0 | 转发数（**原子 UPDATE 数字字段，状态机驱动 ±1**） — 详见下方「计数双写范式」 |
| `viewCount` | BIGINT | DEFAULT 0 | 浏览数（TDynamicViewLog upsert 首次 0→1 时原子 +1） |
| `shareCount` | BIGINT | DEFAULT 0 | 分享数（预留；按明细表 + 计数范式实现） |
| `coinCount` | BIGINT | DEFAULT 0 | 投币数（预留） |
| `favoriteCount` | BIGINT | DEFAULT 0 | 收藏数（预留） |
| `updatedAt` | TIMESTAMPTZ | DEFAULT now(), onupdate=now() | |

> **统一计数双写范式（对 likeCount / repostCount / viewCount / commentCount / shareCount 都适用）**：
>
> 1. **明细表（唯一约束做幂等）**：先写/删明细表（`TDynamicLike` / `TDynamicViewLog` / 评论明细），利用 DB 唯一约束保证"同一行为在同一维度上只发生 1 次语义"。
> 2. **计数原子增减**：明细操作成功**且产生实际语义变化**（新增语义 → +1；撤销语义 → -1）时，紧接着在**同一个数据库事务**里对 `TDynamicStat.<col>` 做 `UPDATE ... SET col = col + delta WHERE dynId = ?`，delta 只能是 ±1，完全依赖 PG 原子 UPDATE 防并发丢失。
> 3. **幂等保护**：明细唯一约束冲突时，跳过计数 UPDATE，整个接口仍然返回成功（而不是报错），因此「用户重试 → 计数不被意外叠加」。
> 4. **补偿对账（非热路径）**：仅在夜间定时或人工运维脚本里做 `COUNT(*) FROM 明细 WHERE 条件` 与 `TDynamicStat` 字段比较，发现不一致则 UPDATE 对齐一次。**请求链路（Feed/详情等）严禁触发 COUNT 聚合查询**。

> **repostCount 状态机触发点（±1 的唯一合法修改点）**：
>
> repostCount 只统计"当前 auditStatus=normal 且 deletedAt IS NULL 的 FORWARD 类型子动态数量"。任何会导致 FORWARD 动态跨越 counting/non-counting 边界的动作，都必须同时修改 `srcDyn.repostCount`：
>
> | # | 动作 | FORWARD 动态 before | FORWARD 动态 after | 是否计数变化 | 对 srcDyn.repostCount 的修改 |
> |---|---|---|---|---|---|
> | ① | 管理员审核通过（P6-T2 approve 接口） | auditing | normal | 进入 counting | +1 |
> | ② | 管理员审核驳回（P6-T3 reject 接口） | normal | rejected | 离开 counting | -1 |
> | ③ | 用户编辑自己的 normal 转发动态（P2-T4 edit 服务） | normal | auditing（重新审核） | 离开 counting | -1；若后续再通过则走①再 +1 |
> | ④ | 用户删除自己的 normal 转发动态（P2-T4 remove 服务软删） | normal | deletedAt ≠ NULL | 离开 counting | -1 |
> | ⑤ | 用户删除 auditing/rejected 转发动态 | 不在 counting | 离开（仍不 counting） | 无变化 | 不改 |
> | ⑥ | 新建 FORWARD 转发时 | - | auditing（默认） | 还没进 counting | 不改（此时**不要** +1，必须等审核通过后才加） |
>
> 注意：repostCount 增减的操作对象永远是 `repostSrcDynId` 指向的**源动态**那一行的 `TDynamicStat.repostCount`，不是转发动态自己的。

> **likeCount 双写链路（点赞幂等）**：
>
> 点赞接口（P4-T3）事务步骤：
> 1. 查动态 `auditStatus='normal' AND deletedAt IS NULL`，否则报错拒绝。
> 2. `INSERT INTO "TDynamicLike" (dynId, mid, likeType, createdAt) VALUES (?, ?, 1, now())`；捕获 `UniqueViolation`：
>    - 若用户意图是 `up=1`（点赞）且冲突 → 视为重复点赞 → 直接返回成功（不做 +1，也不报错）。
>    - 若用户意图是 `up=2`（取消）且冲突不存在 → 返回成功（本来就没点赞）。
> 3. 若 INSERT 成功且是点赞意图 → 同事务里 `UPDATE "TDynamicStat" SET "likeCount" = "likeCount" + 1 WHERE "dynId" = ?`。
> 4. 若用户意图是取消点赞且明细 DELETE 成功（DELETE RETURNING 返回 1 行）→ 同事务里 `UPDATE "TDynamicStat" SET "likeCount" = "likeCount" - 1 WHERE "dynId" = ? AND "likeCount" > 0`（加 `>0` 防极端负数兜底）。
> 5. 事件提醒（P6-T6）在事务提交后 fire-and-forget 生成 LIKE 事件。
>
> `viewCount`（浏览）双写链路与 likeCount 同构，唯一维度由 `UniqueConstraint(dynId, mid, refDate)` 提供，首次 upsert 产生新行才 +1。

### 4.3 动态点赞记录表 `TDynamicLike`

| 列名 | 类型 | 约束 | 说明 |
|---|---|---|---|
| `pk` | BIGINT | PK, autoincrement | |
| `dynId` | BIGINT | FK → `TDynamic.dynId`, NOT NULL | |
| `mid` | BIGINT | FK → `TUserInfo.uid`, NOT NULL | 点赞者 |
| `likeType` | INT | DEFAULT 1 | 点赞类型：1=普通点赞（预留扩展） |
| `createdAt` | TIMESTAMPTZ | DEFAULT now() | |

**约束：** `UniqueConstraint(dynId, mid)` → 一人一赞，**幂等双写的关键**（重复点赞 INSERT 冲突跳过计数）
**索引：** `idx_like_mid_time (mid, createdAt DESC)` → "我赞过的"列表

### 4.4 动态话题表 `TDynamicTopic`

| 列名 | 类型 | 约束 | 说明 |
|---|---|---|---|
| `topicId` | BIGINT | PK, autoincrement | |
| `topicName` | VARCHAR(100) | UNIQUE, NOT NULL | 话题名称 |
| `topicCover` | VARCHAR(1024) | NULL | 话题封面图 |
| `topicDesc` | TEXT | NULL | 话题描述 |
| `jumpUrl` | VARCHAR(1024) | NULL | 话题跳转 H5 |
| `dynCount` | BIGINT | DEFAULT 0 | 话题下动态数（冗余计数，仅统计 auditStatus='normal' 且未软删） |
| `viewCount` | BIGINT | DEFAULT 0 | 话题浏览量 |
| `isHot` | INT | DEFAULT 0 | 是否热门话题 |
| `sortWeight` | INT | DEFAULT 0 | 广场排序权重 |
| `createdAt` | TIMESTAMPTZ | DEFAULT now() | |
| `updatedAt` | TIMESTAMPTZ | DEFAULT now(), onupdate=now() | |

### 4.5 动态浏览去重表 `TDynamicViewLog`

| 列名 | 类型 | 约束 | 说明 |
|---|---|---|---|
| `pk` | BIGINT | PK, autoincrement | |
| `dynId` | BIGINT | NOT NULL | |
| `mid` | BIGINT | NOT NULL | |
| `refDate` | VARCHAR(10) | NOT NULL | 日期 YYYY-MM-DD |
| `viewCount` | INT | DEFAULT 1 | 当日浏览次数 |
| `lastViewAt` | TIMESTAMPTZ | DEFAULT now() | |

**约束：** `UniqueConstraint(dynId, mid, refDate)` → 同一用户同一天只计一次浏览量
**策略：** 先 upsert 此表，只有首次插入（`xmax = 0`）才给 `TDynamicStat.viewCount` +1；后续同日重复调用只自增 `TDynamicViewLog.viewCount`，不累加 `TDynamicStat.viewCount`。

### 4.6 动态举报表 `TDynamicReport`

| 列名 | 类型 | 约束 | 说明 |
|---|---|---|---|
| `pk` | BIGINT | PK, autoincrement | |
| `dynId` | BIGINT | FK → `TDynamic.dynId`, NOT NULL | 被举报动态 |
| `accusedMid` | BIGINT | FK → `TUserInfo.uid`, NOT NULL | 被举报用户（= TDynamic.mid，冗余便于查询） |
| `reportMid` | BIGINT | FK → `TUserInfo.uid`, NOT NULL | 举报人 |
| `reasonType` | INT | NOT NULL | 举报原因类型（ReportReasonEnum 枚举） |
| `reasonDesc` | TEXT | NULL | 补充描述（选填） |
| `auditStatus` | VARCHAR(16) | DEFAULT 'pending' | pending/resolved/rejected |
| `auditRemark` | VARCHAR(500) | NULL | 审核处理备注 |
| `auditAdminMid` | BIGINT | NULL | 处理管理员 MID |
| `createdAt` | TIMESTAMPTZ | DEFAULT now() | |

### 4.7 动态审核记录表 `TDynamicAuditLog`

> 记录每一次审核流转：发布、编辑、管理员通过/驳回。用于管理员后台流水和审计。

| 列名 | 类型 | 约束 | 说明 |
|---|---|---|---|
| `pk` | BIGINT | PK, autoincrement | |
| `dynId` | BIGINT | FK → `TDynamic.dynId`, NOT NULL | 被审核的动态 |
| `operatorMid` | BIGINT | FK → `TUserInfo.uid`, NOT NULL | 操作人 MID（作者=发布/编辑；管理员=通过/驳回） |
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
- `idx_audit_log_dynid_created (dynId, createdAt DESC)` → 单条动态审核历史
- `idx_audit_log_admin_created (operatorMid, createdAt DESC)` → 管理员审核历史
- `idx_audit_log_action_created (actionType, createdAt DESC)` → 按 action 过滤

---

> **MVP 未包含的表（后续迭代补建）**：
> - `TDynamicMedia` — 图片上传/存储；当前仅支持正文中外链图片 URL，不做上传。
> - `TDynamicAttachCard` — 投票/预约/商品等附加大卡配置；当前 MVP 不提供附加卡。
> - 其他 AV/PGC/ARTICLE/LIVE 等业务资源表已在其他模块复用，此处不重复建设。

---

## 五、API 接口规范

所有接口统一放在 `/api/v1/dynamic/` 下，响应格式对齐现有 be-message 规范（`code`/`msg`/`data`），鉴权复用现有 `get_current_user` 依赖（JWT → `x-bili-mid`）。

### 5.1 发布类接口

| 方法 | 路径 | 说明 | 请求体关键字段 |
|---|---|---|---|
| POST | `/create` | 创建动态 | `scene`(WORD/FORWARD), `content`(富文本，支持外链图片 URL 节点), `repostSrc{dynId}`, `topic`, `lbs`, `option{closeComment}` → 响应 `auditStatus='auditing'` |
| POST | `/edit` | 编辑动态 | `dynId`, `scene`, `content`, `option`；rejected/auditing 编辑后自动回 auditing |
| POST | `/remove` | 删除动态（软删） | `dynId` → 设置 `deletedAt` |
| POST | `/repost` | 转发动态（FORWARD） | `srcDynId`(源动态必须 auditStatus='normal'), `content`(转发语) |
| POST | `/space/top` | 空间置顶 | `dynId`（仅本人，且动态 auditStatus='normal'） |
| POST | `/space/untop` | 取消置顶 | `dynId` |
| POST | `/create/check` | 发布页预校验 | `scene` → 返回 `{setting, permission, allowedScenes:['WORD','FORWARD']}` |

### 5.2 Feed 流接口

| 方法 | 路径 | 说明 | 查询参数 |
|---|---|---|---|
| GET | `/feed/all` | 综合页 Feed（仅 normal+未软删） | `updateBaseline`, `historyOffset`, `page`, `refreshType(1=刷新,2=翻页)` |
| GET | `/feed/space/{mid}` | 个人空间 Feed | `hostMid`, `offset`, `page`, `isPreload`；**本人请求**返回 auditing/rejected 带状态标签；**访客请求**过滤为仅 normal |
| GET | `/feed/topic/{topicId}` | 话题 Feed 流 | `topicId`, `offset`, `page`（仅 normal） |
| GET | `/detail/{dynId}` | 动态详情 | `dynId`；本人可看全部状态；访客仅 normal（或返回 404/审核中占位卡） |
| POST | `/details` | 批量动态详情 | `dynamicIds[]`（逗号分隔，限 20 条；按请求者权限过滤） |

> **注意**：所有对外 Feed/详情接口的 WHERE 必须包含 `deletedAt IS NULL`，并按请求者身份决定 auditStatus 过滤条件。视频页 Feed（`/feed/video`）MVP 不实现，后续 DRAW/AV 迭代再开。

**Feed 分页游标方案（对齐 B站）：**

- `updateBaseline`：刷新基线（最新一条 dynId），用于下拉刷新时的"更新了 N 条"
- `historyOffset`：历史偏移（最旧一条 dynId + 时间戳编码），用于上拉加载
- `hasMore`：布尔值，是否还有下一页
- `updateNum`：刷新后新增条数

### 5.3 互动接口

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/thumb` | 点赞/取消点赞（`up: 1=赞, 2=取消`；仅 normal 动态允许） |
| POST | `/report` | 举报动态（`reasonType`, `reasonDesc?`） |
| POST | `/view` | 上报浏览（含去重；仅 normal 动态累加 viewCount） |
| POST | `/attach-card/button` | 附加卡按钮点击（MVP 不实现，返回 501 占位） |
| POST | `/vote` | 动态投票操作（MVP 不实现，同上占位） |

### 5.4 话题 & @ & LBS 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/at/list` | @用户推荐列表（最近联系/关注/粉丝分组） |
| GET | `/at/search` | @用户搜索（keyword 模糊匹配昵称） |
| GET | `/topic/square` | 话题广场列表 |
| GET | `/topic/hot-search` | 热门话题搜索 |
| GET | `/poi/nearby` | 附近地点（lat, lng, page） |
| GET | `/poi/search` | POI 关键词搜索（keyword, lat, lng） |

### 5.5 审核管理接口（管理员权限守卫，路由前缀 `/api/v1/dynamic/audit`）

| 方法 | 路径 | 说明 | 请求体/参数 |
|---|---|---|---|
| GET | `/list` | 管理员待审核列表 | `page`, `pageSize`；默认 WHERE `auditStatus='auditing'`，按 `createdAt DESC` |
| GET | `/list/history` | 审核历史流水 | `dynId?`, `operatorMid?`, `actionType?`, `fromDate`, `toDate`, `page` |
| POST | `/approve` | 审核通过 | `dynId`, `remark?` → 写 `TDynamicAuditLog(action=approve)`；**不发通知**；dynType=FORWARD 时 srcDyn.repostCount +1 |
| POST | `/reject` | 审核驳回 | `dynId`, `rejectReason` → 写 `auditRejectReason` + 审核日志 + **发驳回事件通知给作者**；FORWARD 且 before=normal 时 srcDyn.repostCount -1 |
| GET | `/{dynId}` | 单条动态审核详情（含全部状态 + 历史流转） | `dynId` |

### 5.6 响应示例（动态详情 — WORD 型 + 外链图片节点）

```json
{
  "code": 0,
  "msg": "success",
  "data": {
    "dynId": 1234567890,
    "dynIdStr": "1234567890",
    "dynType": "WORD",
    "auditStatus": "normal",
    "auditRejectReason": null,
    "modules": [
      { "moduleType": "author", "mid": 98765, "uname": "xxx", "face": "...", "ptimeLabelText": "10分钟前", "relation": "following" },
      {
        "moduleType": "desc",
        "text": "今天拍了张好看的照片 @xxx #风景# 链接→https://img.example.com/a.jpg",
        "nodes": [
          {"type": "WORDS", "text": "今天拍了张好看的照片 "},
          {"type": "AT", "bizId": "98765", "name": "xxx"},
          {"type": "WORDS", "text": " "},
          {"type": "TOPIC", "bizId": "123", "name": "风景"},
          {"type": "WORDS", "text": " 链接→"},
          {"type": "LINK", "text": "img.example.com/...", "jumpUrl": "https://img.example.com/a.jpg", "picMeta": {"renderAsImage": true}}
        ]
      },
      { "moduleType": "dynamic", "type": "word", "text": null },
      { "moduleType": "stat", "likeCount": 100, "commentCount": 20, "repostCount": 5, "viewCount": 999 },
      { "moduleType": "interaction", "like": { "isLike": true }, "comment": { "preview": [] } }
    ],
    "extend": { "topicId": 123 }
  }
}
```

---

## 六、开发规范与代码质量标准

> 对齐现有 be-message 规范，参考 [pptr_db.py](file:///home/minato/BilibiliExplosion/be-message-service/app/models/pptr_db.py) & [enums.py](file:///home/minato/BilibiliExplosion/be-message-service/app/models/enums.py)

### 6.1 ORM 模型规范

- **必须** 使用 `SQLModel` 定义表，列名（`name`）使用 camelCase，Python 属性名与数据库列名完全一致
- **必须** 为所有 `updatedAt` 列添加 `onupdate=func.now()`（在 `sa_column_kwargs` 中）
- **必须** 使用 `Field` 参数声明 `nullable`/`primary_key`/`max_length`，`sa_type` 仅用于 `BIGINT`/`DateTime(timezone=True)`/`JSONB` 等特殊类型
- `sa_column_kwargs` **仅**用于：`server_default`、`onupdate`、`comment`、`autoincrement`
- **禁止** 使用 `sa_column=Column(...)` 方式
- 所有外键**必须**显式声明 `ForeignKeyConstraint`，级联策略对齐现有表（用户 FK 用 `CASCADE`）
- 索引**必须**在 `__table_args__` 中显式声明（含命名）
- 所有表名使用 `"T"` 前缀的 PascalCase（如 `"TDynamic"`、`"TDynamicStat"`）

### 6.2 枚举规范

- 数据库存储 **int** 的枚举使用 `IntEnum`（如 `DynamicTypeEnum`）
- 数据库存储 **VARCHAR** 的枚举使用 `StrEnum`（如 `AuditStatusEnum`）
- 对外接口响应**必须**将 int 值转换为 string 名称（如 `dynType: 6 → "WORD"`）
- 新增枚举**必须**追加到 `__all__` 并在 `app/models/__init__.py` 中导出

### 6.3 Service 层规范

- **禁止**在 service 内部吞异常（try-catch 只做包装 + re-raise，不静默失败），错误由统一异常中间件处理
- 跨表写入**必须**使用 `async with session.begin()` 事务，失败自动回滚
- 涉及父子链（如 `TDynamic → TDynamicStat`）的插入，在 `s.add(parent)` 后**必须** `await s.flush()` 再添加子行，避免 `ForeignKeyViolationError`
- ID 生成：`dynId` 使用现有 `UidGenerator`（[sharding.py](file:///home/minato/BilibiliExplosion/be-message-service/app/core/sharding.py)），注意 epoch 单位转换正确（minute-step）
- **计数双写**：明细表操作与计数 UPDATE **必须在同一个事务内**，严禁跨事务分两步写

### 6.4 API 层规范

- **必须** 使用 SQLModel 定义 Request/Response Schema（`app/models/schemas/dynamic.py`），**禁止**裸 dict 返回
- 分页参数**必须**有合理上限（单页最多 50 条）
- 所有写接口**必须**校验权限（仅作者本人可编辑/删除/置顶自己的动态；审核接口仅 root/admin）
- 响应中 dynId 同时提供 `dynId`(int) 和 `dynIdStr`(string)，兼容前端大整数丢失精度

### 6.5 迁移 & 部署规范

- 所有 DDL 通过 Alembic `alembic_pptr/` 分支纳管，**禁止**手写 `CREATE TABLE`
- `app/core/migration.py` 在 lifespan 中自动 `run_alembic_pptr_upgrade()`，无需手动执行
- 删除表如需级联，使用 `op.execute('DROP TABLE IF EXISTS "TDynamic" CASCADE')`，`op.drop_table()` 不支持 cascade
- 检查表存在用 `to_regclass('"TDynamic"')`（带双引号，mixed-case 名称）

### 6.6 质量门禁

- 新增代码**必须**通过 `ruff` lint（或项目内配置的 lint 工具）
- Service 核心逻辑（点赞幂等、Feed 游标、发布事务、repostCount 状态机 ±1）**必须**有单元测试
- 敏感操作（删除动态、置顶、审核通过/驳回）需要打点日志（logger + 操作人 IP/UA，参考 `TUserActInfoLog` 模式）

---

## 七、技术方案决策记录

以下为已确认的技术决策，开发过程中不得擅自偏离：

| # | 议题 | 决策 | 说明 |
|---|---|---|---|
| 1 | 动态 ID 生成方案 | 使用现有 `UidGenerator`(雪花) | 与用户表一致 |
| 2 | 动态正文存储方式 | JSONB 全量存富文本节点 | 对齐 B站 Opus/Paragraph 结构 |
| 3 | Feed 流排序 | 纯按 pubTime DESC（时间倒序） | MVP 简单优先，后续加推荐算法 |
| 4 | 浏览计数粒度 | 同用户同日同动态只计 1 次 | 真实流量 |
| 5 | 评论区复用 | 复用现有评论系统（CommentTypeEnum.DYNAMIC，oid=dynId） | 无重复建设，审核/封禁逻辑通用 |
| 6 | 事件提醒复用 | 复用现有 EventTypeEnum（LIKE/REPLY/AT/AUDIT_REJECT），SourceType=DYNAMIC | 联动消息中心，用户体验一致 |
| 7 | 发布动态经验值 | **不加** | 后续迭代再考虑 |
| 8 | MVP 裁剪范围 | Phase1-4 先上线（发布+Feed+互动+统计），Phase5-7 迭代 | 尽早闭环，快速验证 |
| 9 | 审核流触发方式 | 发布后全量进入审核（严格模式） | 所有动态必须经过审核 |
| 10 | 计数策略（likeCount/repostCount/viewCount 等） | **明细表唯一约束幂等 + 计数原子 UPDATE ±1**（主路径）+ 夜间 COUNT 对账补偿 | 计数直接存数字字段，审核状态/删除等在代码层做 ±1；点赞需同时存明细表记录谁赞了哪条动态 |
| 11 | MVP 动态类型范围 | 仅 WORD + FORWARD（无图片上传，正文外链图） | 图片仅限正文外链 URL，视频/图文均延期 |

---

## 八、任务完成记录表

> 每完成一项任务后，在对应行填写完成时间与说明。格式：`[完成] YYYY-MM-DD HH:MM — 说明`
>
> 状态栏可用值：`待开始` / `进行中` / `完成` / `阻塞` / `跳过（延期）`

### Phase 1（基础骨架 & 数据库）

| 任务 ID | 任务描述 | 状态 | 完成时间 | 说明 |
|---|---|---|---|---|
| P1-T1 | 新增动态相关枚举（DynamicTypeEnum MVP 仅 WORD/FORWARD；AuditStatusEnum 等） | 待开始 | | |
| P1-T2 | 创建 dynamic_db.py ORM 模型（7 表：TDynamic / TDynamicStat / TDynamicLike / TDynamicTopic / TDynamicViewLog / TDynamicReport / TDynamicAuditLog） | 待开始 | | |
| P1-T3 | 在 __init__.py 中导出新模型 | 待开始 | | |
| P1-T4 | 创建 Alembic pptr 迁移脚本（建表 + 索引 + FK） | 待开始 | | |
| P1-T5 | 验证迁移脚本执行 | 待开始 | | |

### Phase 2（发布 CRUD + 审核状态流转）

| 任务 ID | 任务描述 | 状态 | 完成时间 | 说明 |
|---|---|---|---|---|
| P2-T1 | 创建 schemas/dynamic.py（Request/Response Schema） | 待开始 | | |
| P2-T2 | 纯文字动态创建（auditStatus=auditing，WORD 校验，外链图节点支持） | 待开始 | | |
| P2-T3 | 转发动态创建（源动态 must be normal；repostSrcDynId + 深度计算；创建时不 +repostCount） | 待开始 | | |
| P2-T4 | 动态编辑/删除（编辑 rejected/auditing 回 auditing；软删 deletedAt；FORWARD ∧ before=normal 时 srcDyn.repostCount -1） | 待开始 | | |
| P2-T5 | 空间置顶/取消置顶（仅本人且 normal） | 待开始 | | |
| P2-T6 | 发布前置校验（权限/字数/@数量；MVP 拒绝非 WORD/FORWARD） | 待开始 | | |
| P2-T7 | 发布类路由注册（api/dynamic.py → main.py） | 待开始 | | |
| P2-T8 | 单元测试 | 待开始 | | |

### Phase 3（Feed 流 & 详情）

| 任务 ID | 任务描述 | 状态 | 完成时间 | 说明 |
|---|---|---|---|---|
| P3-T1 | 综合页 Feed（仅 normal+未软删；pubTime 倒序） | 待开始 | | |
| P3-T2 | 个人空间 Feed（本人=全部状态；访客=仅 normal；置顶优先） | 待开始 | | |
| P3-T3 | 动态详情页（权限判断；repostCount 直接读 TDynamicStat 字段） | 待开始 | | |
| P3-T4 | 批量动态详情（≤20 条；权限过滤） | 待开始 | | |
| P3-T5 | 分页游标方案（updateBaseline + historyOffset + hasMore + updateNum） | 待开始 | | |
| P3-T6 | Feed 路由注册（api/dynamic_feed.py → main.py） | 待开始 | | |
| P3-T7 | 单元测试 | 待开始 | | |

### Phase 4（互动 & 统计）

| 任务 ID | 任务描述 | 状态 | 完成时间 | 说明 |
|---|---|---|---|---|
| P4-T1 | 原子计数封装（incr/decr_stat）；likeCount/repostCount/viewCount/commentCount 全量原子 UPDATE；批量读取直接读 Stat；禁热路径 COUNT | 待开始 | | |
| P4-T2 | repostCount 状态机 4 触发点 ±1（approve/reject/编辑 normal 回审核/软删 normal 转发） | 待开始 | | |
| P4-T3 | 点赞/取消点赞（幂等；事务 INSERT/DELETE Like + UPDATE likeCount ±1；唯一冲突跳过计数） | 待开始 | | |
| P4-T4 | 浏览上报去重（ViewLog upsert；首次新行才 +viewCount） | 待开始 | | |
| P4-T5 | 动态举报（写 Report；不改变 auditStatus） | 待开始 | | |
| P4-T6 | 夜间/运维对账脚本（明细 COUNT vs Stat 不一致修正；可选 CLI / APScheduler） | 待开始 | | |
| P4-T7 | 互动类路由注册 | 待开始 | | |
| P4-T8 | 单元测试（点赞幂等/浏览去重/repostCount 4 点 ±1/举报写入/计数器负数兜底） | 待开始 | | |

### Phase 5（话题 & @ & LBS）

| 任务 ID | 任务描述 | 状态 | 完成时间 | 说明 |
|---|---|---|---|---|
| P5-T1 | 话题广场列表 | 待开始 | | |
| P5-T2 | 话题 Feed 流 | 待开始 | | |
| P5-T3 | @用户推荐列表 | 待开始 | | |
| P5-T4 | @用户搜索（昵称模糊匹配） | 待开始 | | |
| P5-T5 | POI LBS 附近地点 | 待开始 | | |
| P5-T6 | POI 关键词搜索 | 待开始 | | |
| P5-T7 | 辅助类路由注册 | 待开始 | | |
| P5-T8 | 单元测试 | 待开始 | | |

### Phase 6（审核服务 & 事件联动）

| 任务 ID | 任务描述 | 状态 | 完成时间 | 说明 |
|---|---|---|---|---|
| P6-T1 | 管理员待审核列表（分页；auditStatus=auditing 倒序） | 待开始 | | |
| P6-T2 | 审核通过（→ normal + pubTime=now() + 写 AuditLog + FORWARD 时 srcDyn.repostCount +1；不发通知） | 待开始 | | |
| P6-T3 | 审核驳回（→ rejected + auditRejectReason + AuditLog + 发驳回通知；FORWARD ∧ before=normal 时 srcDyn.repostCount -1） | 待开始 | | |
| P6-T4 | 审核记录流水查询（dynId/管理员/时间段过滤） | 待开始 | | |
| P6-T5 | 管理员权限校验（role 守卫装饰器） | 待开始 | | |
| P6-T6 | 点赞事件提醒（LIKE + DYNAMIC） | 待开始 | | |
| P6-T7 | 评论事件提醒（REPLY + DYNAMIC；复用评论系统链路） | 待开始 | | |
| P6-T8 | @用户事件提醒（AT + DYNAMIC；发布时解析 AT 节点批量生产） | 待开始 | | |
| P6-T9 | 管理员审核路由注册（api/dynamic_audit.py + role 守卫） | 待开始 | | |
| P6-T10 | 单元测试 | 待开始 | | |

### Phase 7（接口联调 & 测试）

| 任务 ID | 任务描述 | 状态 | 完成时间 | 说明 |
|---|---|---|---|---|
| P7-T1 | 全链路冒烟（发布→auditing→管理员通过→Feed→点赞→评论→转发审核通过→repostCount +1→删除转发→repostCount -1） | 待开始 | | |
| P7-T2 | 审核流专项测试（驳回通知到达/编辑 rejected 回 auditing/通过不发通知） | 待开始 | | |
| P7-T3 | 边界用例测试（空内容/超长文本/@上限/转发源非 normal→拒绝/非本人操作） | 待开始 | | |
| P7-T4 | 权限测试（非作者无法编辑删除置顶；非管理员无法调用审核接口） | 待开始 | | |
| P7-T5 | 并发测试（点赞幂等原子性/浏览去重幂等性/repostCount 审核+删除并发 ±1/计数器负数兜底） | 待开始 | | |
| P7-T6 | 性能测试（Feed 流分页/批量详情/Stat 批量 IN 查询 EXPLAIN；对账 COUNT 脚本表现） | 待开始 | | |
| P7-T7 | Bug 修复 & Code Review | 待开始 | | |
| P7-T8 | 接口文档更新 | 待开始 | | |

---

## 附录：参考文件索引

| 文件 | 用途 |
|---|---|
| [pptr_db.py](file:///home/minato/BilibiliExplosion/be-message-service/app/models/pptr_db.py) | 现有 ORM 模型规范参考 |
| [enums.py](file:///home/minato/BilibiliExplosion/be-message-service/app/models/enums.py) | 枚举定义规范参考 |
| [sharding.py](file:///home/minato/BilibiliExplosion/be-message-service/app/core/sharding.py) | 雪花 ID 生成器 |
| [migration.py](file:///home/minato/BilibiliExplosion/be-message-service/app/core/migration.py) | Alembic 迁移入口 |
| [dynamic.proto](file:///home/minato/BilibiliExplosion/be-bilibili-crawler/Service/GrpcModule/Grpc/GrpcProto/bilibili/dynamic/common/dynamic.proto) | B站动态通用模型 |
| [gateway.proto](file:///home/minato/BilibiliExplosion/be-bilibili-crawler/Service/GrpcModule/Grpc/GrpcProto/bilibili/dynamic/gw/gateway.proto) | B站动态网关模型 |
| [api.proto](file:///home/minato/BilibiliExplosion/be-bilibili-crawler/Service/GrpcModule/Grpc/GrpcProto/bilibili/dynamic/interfaces/feed/v1/api.proto) | B站动态 Feed 接口 |
| [v2/dynamic.proto](file:///home/minato/BilibiliExplosion/be-bilibili-crawler/Service/GrpcModule/Grpc/GrpcProto/bilibili/app/dynamic/v2/dynamic.proto) | B站 V2 动态服务 |

---

## 九、评审与签署

### 9.1 评审项清单

| 评审项 | 结论 | 备注 |
|---|---|---|
| 数据模型（基于 B站 Proto 反向推导） | ✅ 通过 | 7 张表覆盖 MVP 范围；TDynamicMedia 等延期 |
| 功能模块划分（9 个模块） | ✅ 通过 | 与现有 be-message 服务边界清晰 |
| 分阶段开发计划（Phase 1-7 + 5 里程碑） | ✅ 通过 | 阶段目标可验收、任务粒度可执行 |
| 数据库设计（表结构/索引/FK/约束） | ✅ 通过 | camelCase 列名、onupdate 时间戳对齐 pptr_db.py 规范 |
| 计数双写范式（明细表 + 原子 ±1 + 对账补偿） | ✅ 通过 | likeCount/repostCount/viewCount/commentCount 统一范式 |
| repostCount 状态机 6 触发点 | ✅ 通过 | 唯一合法修改点已明确 |
| API 接口规范（5 类接口 + 游标分页） | ✅ 通过 | 对齐 be-message 现有响应规范 |
| 审核流程（auditing → normal/rejected） | ✅ 通过 | 通过不发通知，驳回发通知含原因 |
| MVP 裁剪范围（仅 WORD + FORWARD，正文外链图） | ✅ 通过 | 视频/图文/专栏等延期 |
| 开发规范与质量门禁 | ✅ 通过 | ruff + 单元测试 + 敏感操作打点 |
| 技术方案决策记录（11 条） | ✅ 通过 | 冻结为基线，开发中不得擅自偏离 |

### 9.2 基线冻结声明

自本版本（1.0.0）签署之日起：

1. **第二章至第七章**所列技术方案、数据库设计、API 规范、开发规范均为**基线**，开发过程中不得擅自偏离
2. 任何对基线的变更必须按「版本演进规则」升级版本号并更新变更记录表，同时通知评审参与方
3. **第八章 任务完成记录表**为活文档，开发过程中持续更新，不视为基线变更
4. 开发启动以 Phase 1 P1-T1 任务状态变更为「进行中」为标志

### 9.3 签署

| 角色 | 签署人 | 签署日期 | 版本 |
|---|---|---|---|
| 方案撰写 | _（待填）_ | 2026-08-10 | 1.0.0 |
| 技术评审 | _（待填）_ | 2026-08-10 | 1.0.0 |
| 项目负责人 | _（待填）_ | 2026-08-10 | 1.0.0 |

---

> **文档结束** | 版本 1.0.0 | 定稿日期 2026-08-10
