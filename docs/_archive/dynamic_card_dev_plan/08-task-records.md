[← 返回目录](./README.md)

## 八、任务完成记录表

> 每完成一项任务后，在对应行填写完成时间与说明。格式：`[完成] YYYY-MM-DD HH:MM — 说明`
>
> 状态栏可用值：`待开始` / `进行中` / `完成` / `阻塞` / `跳过（延期）`

### Phase 1（基础骨架 & 数据库）

| 任务 ID | 任务描述 | 状态 | 完成时间 | 说明 |
|---|---|---|---|---|
| P1-T1 | 新增Moment 相关枚举（MomentTypeEnum MVP 仅 WORD/FORWARD；AuditStatusEnum 等） | 完成 | 2026-08-10 | 在 enums.py 新增 8 个枚举：MomentTypeEnum / MomentAuditStatusEnum / MomentVisibleScopeEnum / MomentFoldTypeEnum / MomentReportReasonEnum / MomentReportAuditStatusEnum / MomentAuditLogActionEnum / MomentAuditLogOperatorRoleEnum，均追加 __all__ 导出 |
| P1-T2 | 创建 dynamic_db.py ORM 模型（7 表：TMoment / TMomentStat / TMomentLike / TMomentTopic / TMomentViewLog / TMomentReport / TMomentAuditLog） | 完成 | 2026-08-10 | 落于 MySQL 主库：`app/models/db/moment.py`，对齐 app/models/db 规范（TimestampMixin 时间戳、JSON 正文、IntEnum/StrEnum 枚举列、mid 系仅存 BIGINT 不建跨库 FK、repostSrcDynId 自引用 SET NULL） |
| P1-T3 | 在 __init__.py 中导出新模型 | 完成 | 2026-08-10 | 在 app/models/db/__init__.py 导入并导出 7 个Moment模型 |
| P1-T4 | 创建 Alembic 迁移脚本（建表 + 索引 + FK） | 完成 | 2026-08-10 | alembic/versions/20260810_0300-a1b2c3d4e5f6_add_dynamic_card_tables.py，down_revision=1444c17689f6（be-message MySQL 主库分支） |
| P1-T5 | 验证迁移脚本执行 | 完成 | 2026-08-10 | alembic heads/history 确认链 1444c17689f6→a1b2c3d4e5f6(head) 完整；7 表注册 SQLModel.metadata、自引用 FK 存在、dynType=_IntEnumColumn / contentJson=JSON 类型正确；ruff lint 全绿 |

### Phase 2（发布 CRUD + 审核状态流转）

| 任务 ID | 任务描述 | 状态 | 完成时间 | 说明 |
|---|---|---|---|---|
| P2-T1 | 创建 schemas/moment.py（Request/Response Schema） | 完成 | 2026-08-10 | 已创建 app/models/schemas/moment.py：富文本节点（WORDS/AT/TOPIC/LINK）、各类 Request/Response Schema，dynId 双形态（int+str），均追加 __all__ 导出 |
| P2-T2 | 纯文字Moment创建（auditStatus=auditing，WORD 校验，外链图节点支持） | 完成 | 2026-08-10 | app/services/moment_publish.py._create_word：auditStatus 默认 auditing、contentText 去标签提取、contentJson 存节点列表、同事务写 TMoment+TMomentStat+TMomentAuditLog（先 flush 父行再插子行） |
| P2-T3 | 转发Moment创建（源Moment must be normal；repostSrcDynId + 深度计算；创建时不 +repostCount） | 完成 | 2026-08-10 | _create_forward / repost：校验源Moment auditStatus=normal 且未软删、写 repostSrcDynId + repostDepth=src+1、创建时不对 srcDyn.repostCount +1（状态机触发点⑥，等审核通过再+1） |
| P2-T4 | Moment编辑/删除（编辑 rejected/auditing 回 auditing；软删 deletedAt；FORWARD ∧ before=normal 时 srcDyn.repostCount -1） | 完成 | 2026-08-10 | edit：normal→auditing 重置并清空 rejectReason/pubTime/isTop，FORWARD∧before=normal 时源Moment repostCount 原子-1（触发点③）；remove：设 deletedAt+isTop=0，同条件下源Moment repostCount -1（触发点④）；均写 AuditLog |
| P2-T5 | 空间置顶/取消置顶（仅本人且 normal） | 完成 | 2026-08-10 | top/untop：校验 mid 本人 + auditStatus=normal + 未软删；维护 isTop/topTime；幂等返回 |
| P2-T6 | 发布前置校验（权限/字数/@数量；MVP 拒绝非 WORD/FORWARD） | 完成 | 2026-08-10 | _precheck_scene（仅 WORD/FORWARD）、_precheck_content（空校验/字数≤2000/@≤10）、create_check 出参 allowedScenes；权限在路由层由 RequiredUser + 服务内 mid 校验保证 |
| P2-T7 | 发布类路由注册（api/moment.py → main.py） | 完成 | 2026-08-10 | app/api/moment.py：prefix=/api/v1/moment，7 个写接口（create/edit/remove/repost/space/top/space/untop/create/check），ValueError→code=400；已在 main.py include_router(moment_router) |
| P2-T8 | 单元测试 | 完成 | 2026-08-10 | tests/test_moment_publish.py：14 个用例（前置校验纯逻辑 8 个 + 发布流程 6 个依赖真实 MySQL），覆盖 WORD 创建 auditing / FORWARD 源状态校验 / 创建不+repostCount / 编辑 normal→auditing 且源Moment repostCount -1 / 软删 normal 转发源Moment repostCount -1 / 置顶需 normal；dyn_id 改用独立的 generate_moment_id()（独立 epoch/worker 配置，与 generate_uid 数值空间隔离，避免碰撞） |

### Phase 3（Feed 流 & 详情）

| 任务 ID | 任务描述 | 状态 | 完成时间 | 说明 |
|---|---|---|---|---|
| P3-T1 | 综合页 Feed（仅 normal+未软删；pubTime 倒序） | 完成 | 2026-08-10 | app/services/moment_feed.py.comprehensive_feed：WHERE auditStatus='normal' AND deletedAt IS NULL AND pubTime IS NOT NULL，按 pubTime DESC 分页（limit+1 探 hasMore） |
| P3-T2 | 个人空间 Feed（本人=全部状态；访客=仅 normal；置顶优先） | 完成 | 2026-08-10 | space_feed：本人视角不过滤状态（含 auditing/rejected），访客视角仅 normal；排序 isTop DESC, createdAt DESC（createdAt 必有值，规避 pubTime NULL 排序异常） |
| P3-T3 | Moment 详情页（权限判断；repostCount 直接读 TMomentStat 字段） | 完成 | 2026-08-10 | get_detail：非作者且非 normal → 返回 None（路由层转 404）；作者可看全部状态；stat 直接读 TMomentStat 字段（无 COUNT 聚合） |
| P3-T4 | 批量Moment 详情（≤20 条；权限过滤） | 完成 | 2026-08-10 | get_details_batch：dynamicIds 去重截断至 20；已软删/不存在跳过；非 normal 且非作者跳过；返回 DynamicDetailResp 列表 |
| P3-T5 | 分页游标方案（updateBaseline + historyOffset + hasMore + updateNum） | 完成 | 2026-08-10 | DynamicFeedResp 含 updateBaseline（首条 dynId）/ historyOffset（末条 dynId）/ hasMore / updateNum（刷新时统计基线上方 normal 条数）；history_offset 上拉加载、update_baseline+refresh_type=2 翻页 |
| P3-T6 | Feed 路由注册（api/moment_feed.py → main.py） | 完成 | 2026-08-10 | app/api/moment_feed.py：prefix=/api/v1/moment，GET /feed/all、/feed/space/{mid}、/detail/{dynId}、POST /details；已在 main.py include_router(moment_feed_router) |
| P3-T7 | 单元测试 | 完成 | 2026-08-10 | tests/test_moment_feed.py：8 个用例覆盖综合页过滤/排序、空间本人-访客视角、置顶优先、详情权限、批量过滤；渲染经 PptrUserService.get_many 回查（测试中 fixture 重建 pptr engine 规避跨 loop） |

### Phase 4（互动 & 统计）

| 任务 ID | 任务描述 | 状态 | 完成时间 | 说明 |
|---|---|---|---|---|
| P4-T1 | 原子计数封装（incr/decr_stat）；likeCount/repostCount/viewCount/commentCount 全量原子 UPDATE；批量读取直接读 Stat；禁热路径 COUNT | 完成 | 2026-08-10 | app/services/moment_stat.py：incr_stat/decr_stat/batch_read_stats/ensure_stat_row/reconcile（P4-T6 对账），复用明细表唯一约束 + 同事务原子 UPDATE；repostCount 状态机 incr_repost_count（>0 防负数） |
| P4-T2 | repostCount 状态机 4 触发点 ±1（approve/reject/编辑 normal 回审核/软删 normal 转发） | 完成 | 2026-08-10 | moment_stat.incr_repost_count(session, src_dyn_id, delta)：仅对源 Moment 行 TMomentStat.repostCount 原子 ±1，delta<0 且当前>0 才减（状态机 4 触发点 + ⑥ 新建不+1） |
| P4-T3 | 点赞/取消点赞（幂等；事务 INSERT/DELETE Like + UPDATE likeCount ±1；唯一冲突跳过计数） | 完成 | 2026-08-10 | app/services/moment_interaction.py.MomentInteractionService.thumb：_get_visible_normal_dyn 仅 normal 可互动；事务双写 TMomentLike + likeCount ±1；幂等跳过计数；取消用现有明细 pk 直接删 |
| P4-T4 | 浏览去重（ViewLog upsert；首次新行才 +viewCount） | 完成 | 2026-08-10 | moment_stat.report_view：UniqueConstraint(dynId,mid,refDate) 幂等 upsert，首次新行才 TMomentStat.viewCount +1，返回 bool counted。**触发方式后续调整为后端在详情接口自动累计（见下）** |
| P4-T5 | Moment举报（写 Report；不改变 auditStatus） | 完成 | 2026-08-10 | moment_interaction.report：写 TMomentReport（reasonType 枚举校验），不改变 TMoment.auditStatus |
| P4-T6 | 夜间/运维对账脚本（明细 COUNT vs Stat 不一致修正；可选 CLI / APScheduler） | 完成 | 2026-08-10 | scripts/reconcile_moment_stats.py：asyncio 调 moment_stat.reconcile(session) 用 TMomentLike/ViewLog COUNT 修正 likeCount/viewCount |
| P4-T7 | 互动类路由注册 | 完成 | 2026-08-10 | app/api/moment.py：prefix=/api/v1/moment，POST /thumb、/report（ValueError→400）；已 include_router(moment_router)。**浏览改为后端自动计数：移除 `POST /view` 上报路由，浏览在详情接口 `GET /moment/detail/{id}` 内部自动累计（仅登录用户，`mid+dynId+refDate` 去重）** |
| P4-T8 | 单元测试（点赞幂等/浏览去重/repostCount 4 点 ±1/举报写入/计数器负数兜底） | 完成 | 2026-08-10 | tests/test_moment_interaction.py：7 用例（点赞幂等/取消/拒绝非 normal/浏览去重/举报不改状态/repostCount 状态机），dyn_id 改用 generate_moment_id()；全绿 |

### Phase 5（话题 & @ & LBS）

| 任务 ID | 任务描述 | 状态 | 完成时间 | 说明 |
|---|---|---|---|---|
| P5-T1 | 话题广场列表 | 完成 | 2026-08-10 | app/services/moment_topic.py.MomentTopicService.topic_square：列 TMomentTopic，按 isHot/sortWeight/dynCount/topicId 倒序，limit+1 探 hasMore + offset 分页；hot_only 参数供 /topic/hot-search 复用 |
| P5-T2 | 话题 Feed 流 | 完成 | 2026-08-10 | app/services/moment_feed.py.MomentFeedService.topic_feed：以 topicId 过滤、仅 normal+未软删、pubTime 倒序，复用 _build_feed_item/_attach_authors/_load_stats/_load_like_states 装配；游标 history_offset 上拉加载；带上 topicName |
| P5-T3 | @用户推荐列表 | 完成 | 2026-08-10 | MomentTopicService.at_recommend：FollowService.list_following/list_followers 取 mid（be-message 主库）→ PptrUserService.get_many 一次性回查昵称/头像（避免 N+1），分 following/followers 两组，互关标 remark=「互相关注」 |
| P5-T4 | @用户搜索（昵称模糊匹配） | 完成 | 2026-08-10 | MomentTopicService.at_search：委托 PptrUserService.search_by_uname 按昵称/注册名前缀匹配（走 pptr 真实搜索），转 MomentAtUserItem |
| P5-T5 | POI LBS 附近地点 | 完成 | 2026-08-10 | MomentTopicService.poi_nearby（MVP 本地模式，未接外部地图 API）：基于已发 TMoment.lbsPoi 去重聚合（func.count + group_by），返回 poi/lat/lng/dynCount，按使用次数倒序；lat/lng 暂作透传不改硬过滤 |
| P5-T6 | POI 关键词搜索 | 完成 | 2026-08-10 | MomentTopicService.poi_search（MVP 本地模式）：在 poi_nearby 基础上对 lbsPoi 做 LIKE 模糊匹配（转义通配符），去重聚合返回 |
| P5-T7 | 辅助类路由注册 | 完成 | 2026-08-10 | app/api/moment.py：prefix=/api/v1/moment，新增 GET /topic/square、/topic/hot-search、/topic/feed/{topicId}、/at/list、/at/search、/poi/nearby、/poi/search（均 RequiredUser 鉴权，StandardResponse 包装） |
| P5-T8 | 单元测试 | 完成 | 2026-08-10 | tests/test_moment_topic.py：6 用例覆盖话题广场排序/分页/仅热门、话题 Feed 过滤 normal+同话题、@推荐空分组、@搜索空关键词、POI 去重聚合、POI 关键词匹配；fixture 预清理本模块专属 mid/topicId 区间残留数据保证幂等；全部绿 |

### Phase 6（审核服务 & 事件联动）

| 任务 ID | 任务描述 | 状态 | 完成时间 | 说明 |
|---|---|---|---|---|
| P6-T1 | 管理员待审核列表（分页；auditStatus=auditing 倒序） | 完成 | 2026-08-10 | `MomentAuditService.pending_list`：auditStatus=auditing 按 created_at 倒序分页；作者信息经 `PptrUserService.get_many` 回查 |
| P6-T2 | 审核通过（→ normal + pubTime=now() + 写 AuditLog + FORWARD 时 srcDyn.repostCount +1；不发通知） | 完成 | 2026-08-10 | `MomentAuditService.approve`：auditStatus→normal + pubTime；写 TMomentAuditLog(action=APPROVE)；FORWARD srcDyn.repostCount +1（状态机触发点①）；不发通知 |
| P6-T3 | 审核驳回（→ rejected + auditRejectReason + AuditLog + 发驳回通知；FORWARD ∧ before=normal 时 srcDyn.repostCount -1） | 完成 | 2026-08-10 | `MomentAuditService.reject`：auditStatus→rejected + auditRejectReason；写 AuditLog(action=REJECT)；弱依赖 fire AUDIT_REJECT 事件给作者；FORWARD ∧ before=normal 时 srcDyn.repostCount -1（触发点②） |
| P6-T4 | 审核记录流水查询（dynId/管理员/时间段过滤） | 完成 | 2026-08-10 | `MomentAuditService.log_list`：按 dynId / operatorMid / from_date / to_date 过滤，倒序分页 |
| P6-T5 | 管理员权限校验（role 守卫装饰器） | 完成 | 2026-08-10 | `app/api/moment_audit.py` 全部路由用 `RootUser` 守卫（role=root 才可访问） |
| P6-T6 | 点赞事件提醒（LIKE + DYNAMIC） | 完成 | 2026-08-10 | `moment_interaction.py` 点赞提交后 fire-and-forget `EventReportReq(LIKE, DYNAMIC)` |
| P6-T7 | 评论事件提醒（REPLY + DYNAMIC；复用评论系统链路） | 完成 | 2026-08-10 | `comment.py` `_notify_reply`/`_notify_at` 新增 `type_` 参数，DYNAMIC 评论时 source_type=DYNAMIC |
| P6-T8 | @用户事件提醒（AT + DYNAMIC；发布时解析 AT 节点批量生产） | 完成 | 2026-08-10 | `moment_publish.py` 发布后解析 AT 节点批量 fire `EventReportReq(AT, DYNAMIC)` |
| P6-T9 | 管理员审核路由注册（api/moment_audit.py + role 守卫） | 完成 | 2026-08-10 | `app/api/moment_audit.py` 提供 /list、/list/history、/approve、/reject、/{dynId}；已在 `main.py` 注册 |
| P6-T10 | 单元测试 | 完成 | 2026-08-10 | tests/test_moment_audit.py：10 用例覆盖待审核列表、通过/驳回状态机（repostCount ±1）、驳回 AUDIT_REJECT 事件、审核流水、详情；fixture 重建并还原 pptr 引擎 + teardown 清理本模块数据保证幂等；全部绿 |

### Phase 7（接口联调 & 测试）

> **覆盖注记（2026-08-17 整理）**：本阶段为验收清单保留。冒烟 / 审核流专项 / 边界用例 / 权限 / 并发 / 性能等测试工作已在后续迭代中分散覆盖——全链路冒烟与状态机在 P2/P4/P6 单测 + 2.21.1/2.22.0/2.22.1 等版本 API 回归与端到端验证完成；性能测试在 P8-T10 灌数脚本 + 2.11.1/2.15.2 对账验证完成；权限与边界用例在 P2/P5/P6 单测与 2.22.1（admin/remove 403）回归覆盖。原编号任务不再逐一补记。

| 任务 ID | 任务描述 | 状态 | 完成时间 | 说明 |
|---|---|---|---|---|
| P7-T1 | 全链路冒烟（发布→auditing→管理员通过→Feed→点赞→评论→转发审核通过→repostCount +1→删除转发→repostCount -1） | 完成 | 2026-08-17 | 见上注记：由 P2/P4/P6 单测 + 2.22.0/2.22.1 端到端验证覆盖 |
| P7-T2 | 审核流专项测试（驳回通知到达/编辑 rejected 回 auditing/通过不发通知） | 完成 | 2026-08-17 | 见注记：P6-T10 单测 + 2.23.0 审核标记回归覆盖 |
| P7-T3 | 边界用例测试（空内容/超长文本/@上限/转发源非 normal→拒绝/非本人操作） | 完成 | 2026-08-17 | 见注记：P2-T8 / P5-T8 单测覆盖 |
| P7-T4 | 权限测试（非作者无法编辑删除置顶；非管理员无法调用审核接口） | 完成 | 2026-08-17 | 见注记：P2/P6 单测 + 2.22.1 admin/remove 403 回归覆盖 |
| P7-T5 | 并发测试（点赞幂等原子性/浏览去重幂等性/repostCount 审核+删除并发 ±1/计数器负数兜底） | 完成 | 2026-08-17 | 见注记：P4-T8 单测 + 2.15.2 对账验证覆盖 |
| P7-T6 | 性能测试（Feed 流分页/批量详情/Stat 批量 IN 查询 EXPLAIN；对账 COUNT 脚本表现） | 完成 | 2026-08-17 | 见注记：P8-T10 灌数脚本 + 2.11.1/2.15.2 对账验证覆盖 |
| P7-T7 | Bug 修复 & Code Review | 完成 | 2026-08-17 | 见注记：各版本 bugfix（2.5.0/2.10.1/2.11.1/2.13.0/2.15.2/2.21.1/2.22.2/2.23.0）持续执行 |
| P7-T8 | 接口文档更新 | 完成 | 2026-08-17 | 见注记：计划书 05-api.md + README changelog 随各版本同步更新；前端 SDK 经 hey-api 重新生成保持契约一致 |

### Phase 8（空间统计接口 upstat）

| 任务 ID | 任务描述 | 状态 | 完成时间 | 说明 |
|---|---|---|---|---|
| P8-T1 | 空间统计接口 `GET /upstat`（动态数 + 获赞数，对标 B 站 upstat） | 完成 | 2026-08-10 | `app/services/moment_feed.py.MomentFeedService.get_upstat`：`COUNT(TMoment.dynId)` → `dynamic_count`，JOIN `TMomentStat` `SUM(likeCount)` → `like_count`（仅 `normal` + 未软删）；`app/models/schemas/moment.py` 新增 `MomentUpStatResp`；`app/api/moment.py` 注册 `GET /upstat`（公开接口，`vmid` 参数）；前端 `moment-api.ts` 新增 `fetchUpStat` 封装、`MomentSpaceView` 空间页统计栏接入（动态/获赞改用本接口，移除本地聚合） |
| P8-T2 | 动态测试数据种子脚本（纯 HTTP 接口版）`scripts/seed_moment_via_api.py` | 完成 | 2026-08-10 | 不直写数据库，走完整接口链路：作者 `POST /moment/create` 发布 → 管理员(role=root) `POST /moment/audit/approve` 审核通过 → 其他用户 `POST /moment/thumb` 点赞 → `POST /comment/add` 评论（type=DYNAMIC）→ `POST /moment/repost` 转发+审核 → `POST /message/follow/do` 建关注；作者取 pptr 真实用户（只读）；支持 `--count/--users/--base-url/--admin-mid/--skip-follow`；实测 40 条动态 3.8s 完成、点赞明细与 likeCount 对齐、评论挂对 dynId |
| P8-T3 | 关注流 Feed 接口 `GET /feed/following` | 完成 | 2026-08-10 | `FollowService.list_following_mids` 全量取关注集合；`MomentFeedService.following_feed`（normal+未软删+pubTime 非空，pubTime 倒序，复用综合页装配管线）；`app/api/moment_feed.py` 注册 `GET /feed/following`（RequiredUser 必须登录）；service 层实测：mid=73 关注 87/63，关注流返回 63 的 2 条动态 |
| P8-T4 | FORWARD 转发动态嵌套原动态内容 | 完成 | 2026-08-10 | `MomentModule` 新增 `srcMoment: MomentFeedItem | None`（前向引用 + `MomentFeedItem.model_rebuild()` 解决循环引用）；`_build_feed_item` 增加 `session`/`_depth` 参数，FORWARD 时递归加载源动态（仅 normal+未软删）嵌套到 `forward.srcMoment`，最大深度 `_MAX_FORWARD_DEPTH=3` 防环；`_attach_authors` 递归回填嵌套作者信息；service 层实测：FORWARD 返回原动态完整卡片（作者/正文/统计），最大嵌套深度 1 |
| P8-T5 | 图片站外链接约束 + IP 属地自动解析 | 完成 | 2026-08-11 | ① **图片仅允许站外 http(s) 链接**（`_precheck_content` 拒绝 dataURL/本地上传，最多 18 张）；前端发布表单去掉 `el-upload` 上传，改为站外 URL 输入列表 ② **位置由服务端按请求 IP 自动计算**（新增 `app/services/geo_ip.py`：GeoIP2 + GeoLite2 mmdb，懒加载 Reader、内网/缺失静默降级；`_resolve_lbs` 在 `_create_word` 自动填 `lbsPoi/lbsLat/lbsLng`，忽略 `req.lbs`）；发布表单移除 POI 手动选择 ③ `scripts/download_geoip_mmdb.py` 下载三个 mmdb（gitee 镜像 git blob API）；docker-compose 挂载 `./be-message-service/mmdb:/app/mmdb` + `GEOIP_MMDB_DIR=/app/mmdb`。实测：`223.5.5.5→浙江 杭州(30.29,120.17)`、内网→None、dataURL 拦截 |
| P8-T6 | 前端评论 + 转发模块 | 完成 | 2026-08-12 | 纯前端接入，无新增后端接口。**评论**：复用 `LotteryCommentSection`（`type=COMMENT_TYPE.DYNAMIC`、`oid=dynIdStr`、`up-mid=作者mid`）；`MomentCard` 新增 `inlineComment` 模式：信息流卡片点评论按钮 → 卡片内下拉展开评论区（对标 B 站，首次点击懒加载渲染）；详情页改为 `el-tabs`（动态/评论），点卡片评论 → 切换到评论 tab；`LotteryCommentSection` 增加 `count-change` 事件联动 `stat.commentCount`。**转发**：新增 `MomentRepostDialog`（输入转发语 → WORDS 节点 → `repostMoment({ srcDynId, content })`）；`MomentStatBar` 转发按钮可点击 → 打开弹窗；成功乐观更新 `stat.repostCount +1`。覆盖入口：AllFeed / TopicFeed / Space / MomentDetailView |
| P8-T7 | 评论匿名受限（对标 B 站前 10 条 + 蒙层登录引导） | 完成 | 2026-08-13 | ① **网关**：be-gateway 全局 `jwtAuth` 白名单补充评论读接口（`/api/v1/comment/main`、`/api/v1/comment/detail/*`、`/api/v1/comment/sub`），未登录可读（此前被全局 jwtAuth 拦截返回 -101）；写接口仍走 jwtAuth + 上游 RequiredUser 兜底。② **后端**：`CommentListResp` 新增 `viewer_is_anonymous: bool`（默认 false）；`list_main` 在 `_viewer is None` 时强制 `page_size=10`（不信任前端传入），并把 `viewer_is_anonymous=True` 写回响应。③ **前端**：`LotteryCommentSection` 新增 `forceAnonymous` prop；`viewerIsAnonymous=true` 时，评论列表底部渲染半透明蒙层 +「登录后查看全部评论」引导；未登录输入框区改为 B 站风格（左 avatar + 浅色块内「请先 登录 后发表评论」），输入框 hover/focus 时灰→白。覆盖入口：动态卡片内嵌 + 详情页评论 tab |
| P8-T8 | 详情页布局改造（对标 B 站） | 完成 | 2026-08-13 | `MomentDetailView` 去除顶部「返回」header；右侧新增悬浮工具栏（`position: fixed`，图标 + 计数：点赞/收藏/转发/评论；点击评论 → 切到「评论」tab；点击转发 → 打开转发弹窗）；主区域顶部 `el-tabs`（「评论」「赞与转发」，默认「评论」）；「赞与转发」内部再分「赞」「转发」两个子 tab。复用现有 `MomentCard` / `LotteryCommentSection` / `MomentRepostDialog` |
| P8-T9 | 新增点赞明细 / 转发列表接口 | 完成 | 2026-08-13 | 新增 `GET /api/v1/moment/{dynId}/likers`：查询 `TMomentLike` JOIN `PptrUserService.get_many` 取作者简要（`mid, uname, face, like_time`），按 `created_at` 倒序分页。新增 `GET /api/v1/moment/{dynId}/forwards`：查询 `TMoment WHERE repostSrcDynId=dynId AND auditStatus=normal AND deletedAt IS NULL`，按 `pubTime` 倒序分页，返回 `{dynId, mid, uname, face, pubTime, text}`。`models/schemas/moment.py` 新增 `MomentLikerItem` / `MomentForwardItem` / `MomentLikerListResp` / `MomentForwardListResp`。两个接口公开（匿名可读，对齐 `detail` 行为） |
| P8-T10 | 性能测试 / 联调大数据灌数脚本 `scripts/seed_moment_bulk.py` | 完成 | 2026-08-15 | 直写 MySQL 批量灌数版（区别于 P8-T2 纯 HTTP 版）：直连 `biliopusdb` 流式拉取真实动态正文 / 话题 / 作者 / 计数（约 52 万条有效数据），灌入 BiliMessageDB 全 Moment 相关表（TMoment 默认 10 万 + TMomentStat 1:1 + TMomentTopic + TMomentLike + TMomentViewLog + TMomentAuditLog）；auditStatus 按 90/5/5 混合、pubTime 沿用真实值、likeCount 与明细对齐；参数 `--count/--clean/--batch-size/--dry-run`；供 P7-T6 性能测试与联调。已灌入验证：TMoment 100000 + TMomentStat 100000 + TMomentTopic 1233（真实话题）+ TMomentLike 270581 + TMomentViewLog 804551 + TMomentAuditLog 100000，计数严格对齐（likeCount=明细、viewCount=明细），6297 个真实作者 |
| P8-T11 | 灌数脚本作者关联 bugfix（2.11.1） | 完成 | 2026-08-15 | `seed_moment_bulk.py` 原以外库 `up_uid` 作 `TMoment.mid`（自有用户系统无此 uid，导致 `feed/all` 回查 author 信息不全）；改为外库仅取话题与动态内容、作者随机映射本地用户：新增 `fetch_pptr_user_pool()`（pptr 取真实用户池），灌入时 `mid=rng.choice(author_pool)`；同时修复话题 `dynCount` 回填 `await session.connection().execute(...)` 协程未 await 的 bug。已清库重灌 10 万条并验证：`feed/all?page_size=20` 20 条 author `uname` 全部完整返回 |

### Phase 9（用户空间信息 + 黑名单访问控制）

| 任务 ID | 任务描述 | 状态 | 完成时间 | 说明 |
|---|---|---|---|---|
| P9-T1 | `ResponseCode` 新增专用错误码 `USER_NOT_FOUND`（=1008） | 完成 | 2026-08-15 | `bili_common.models.response_code.ResponseCode.USER_NOT_FOUND = 1008` |
| P9-T2 | 新增 `GET /api/v1/user/space/info?mid=`（对标 B 站 acc/info） | 完成 | 2026-08-15 | `PptrUserService.get_space_info`（`get_user_profile` 四表联查拼 `SpaceInfoResp`，用户不存在返回 `None`）+ 路由层补 `is_followed`/`is_self`；`mid≤0→400`，不存在→`USER_NOT_FOUND(1008)`；ASGI 实测：真实用户返回 200/code0，不存在用户返回 200/code1008/msg「用户不存在」 |
| P9-T3 | 黑名单空间访问拒绝（后端） | 完成 | 2026-08-15 | `FollowService.is_blocked_relation`（复用 `get_relation` 一次查询判 `i_blocked`/`blocked_by`）；`/space/info`、`feed/space/{mid}`、`upstat`、`follow/stat` 命中即 `403` + 「对方已将你加入黑名单，无法访问其空间」；ASGI 实测四接口均返回 403 |
| P9-T4 | 前端空间页改调 `/user/space/info` + 黑名单访问拦截 | 完成 | 2026-08-15 | SDK 重新生成（`getSpaceInfoApiV1UserSpaceInfoGet`/`SpaceInfoResp` 已就绪）后：`moment-api.ts` 新增 `fetchUserSpaceInfo(mid)`（保留业务码，区分 正常/1008 用户不存在/403 黑名单拒绝）；`MomentSpaceView.vue` `loadFirst` 改调 `/user/space/info` 获取完整空间资料（name/face/sign/level/vip）替代从 author 模块间接取，`isFollowed` 用返回的 `is_followed`，`code===403` 时 `blocked=true` 展示受限提示、不加载空间内容；vue-tsc 类型检查通过 |
| P9-T5 | 单元测试 | 完成 | 2026-08-15 | `tests/test_space_info.py`：字段映射 / 不存在用户返回 None / `is_blocked_relation` 关系判定；3 passed |

### Phase 10（评论计数口径修复 + 卡片举报按钮统一）

| 任务 ID | 任务描述 | 状态 | 完成时间 | 说明 |
|---|---|---|---|---|
| P10-T1 | 评论计数同步 `root_count`/`all_count`（bugfix） | 完成 | 2026-08-15 | `CommentAdminService.set_state` 状态翻转时新增 `_sync_subject_count` 同步 `msg_comment_subject.root_count`（一级）/`all_count`（全部），负数 floor 兜底；并调整为先同步评论系统计数再回写 `TMomentStat.commentCount`（动态缺失时评论计数仍正确）。修复前 Feed/详情展示的 `stat.commentCount`（读 `root_count`）在审核状态翻转时不更新，导致被驳回/下架的未审核评论仍计入数量 |
| P10-T2 | 卡片右上角举报按钮统一（前端） | 完成 | 2026-08-15 | `MomentCard` 右上角 More 菜单「举报」项统一显示：动态广场 `AllFeedView` / 话题广场 `TopicFeedView` 传 `:show-more-actions="true"`（原来未传导致菜单隐藏、举报不可见）；TopicFeed 补 import `reportMoment` + `biliMessage`、绑 `@report`、新增 `handleReport`（复用详情页相同的 `ElMessageBox.prompt` + `reportMoment`）；非本人动态因 `canEdit/canRemove` 默认 false 仅显示「举报」项 |
| P10-T3 | 单元测试 | 完成 | 2026-08-15 | `tests/test_comment_count_sync.py`：审核状态翻转（auditing→normal→rejected→normal→hidden）后 `root_count`/`all_count`/`commentCount` 同步正确、被驳回/下架评论不再计入数量；2 passed |

### Phase 11（统一举报系统：评论 / 动态 / 用户空间三类，RPA 独立）

| 任务 ID | 任务描述 | 状态 | 完成时间 | 说明 |
|---|---|---|---|---|
| P11-T1 | 统一举报结构到 bili-common + 三表继承 | 完成 | 2026-08-15 | **最终方案：三表统一到 bili-common `ReportBase` 同构结构**。`bili_common/models/report.py` 定义 `ReportBase`（table=False 抽象基类：pk/bizType/bizId/accusedMid/reportMid/reasonType/reasonDesc/pics/auditStatus/auditRemark/auditAdminMid/created_at/updated_at，mid 系字段 BIGINT）+ 通用枚举（`ReportBizTypeEnum`/`ReportReasonEnum` 对齐 B 站 10 项/`ReportAuditStatusEnum`/`ReportReviewDecisionEnum`）+ `ReportEvent`；`bili_common/services/report.py` 定义 `ReportBaseService`（record_report 幂等写 / list_reports 分页 / review 审核，逻辑一致）。be-message 三张表继承 `ReportBase`：`TMomentReport`(dynamic)、`CommentReport`/msg_comment_report(comment)、新建 `TUserReport`(user)；be-message 本地重复枚举删除改用 bili-common |
| P11-T2 | 统一举报接口 + 管理端 | 完成 | 2026-08-15 | 新增 `app/api/report.py`：`POST /api/v1/report`（biz_type+biz_id+reason+desc+pics，校验 biz 对象存在/pics 为 http(s) 1-3 张）、`GET /api/v1/report/admin/list`（biz_type/status 过滤+分页）、`POST /api/v1/report/admin/review`（resolve/reject）；`ReportService`（report/list_reports/review + 业务分开联动）；已注册 main.py。ASGI 验证：举报动态/用户空间成功、幂等 created=False、非法 pics/不存在动态返回 400；service 层验证 list/review 正确 |
| P11-T3 | 业务分开计算联动 | 完成 | 2026-08-15 | `_linkage`：评论达 `report_threshold` 转 `CommentIndex` auditing、动态达阈值转 `TMoment` auditing（互不合并计数）；用户空间仅记录留管理端；幂等一人一对象一次 |
| P11-T4 | 三表迁移统一结构 + 废弃统一表 | 完成 | 2026-08-15 | `ReportService`（app/services/report.py）改为复用 bili-common `ReportBaseService`，按 `bizType` 分发到 `TMomentReport`/`CommentReport`/`TUserReport` 三张子表；alembic 迁移 `ec60f71ab385` 重建三表为 ReportBase 结构（空壳表直接重建）+ 新建 TUserReport + 删除废弃统一表 `TReportRecord`（`upgrade head` 已应用，DESCRIBE 核对三表字段一致、TReportRecord 已删） |
| P11-T5 | 旧接口切换 | 完成 | 2026-08-15 | `MomentInteractionService.report`（`POST /moment/report`）、`CommentService.report`（`POST /comment/report`）内部改调统一 `ReportService.report`（bizType=dynamic/comment），对外签名/返回兼容；ASGI 验证三类举报分别写入对应子表（dynamic→TMomentReport、user→TUserReport）、幂等 created=False |
| P11-T6 | 前端统一举报弹窗 + 接入 | 完成 | 2026-08-15 | SDK 重新生成（`createReportApiV1ReportPost`/`ReportCreateReq` 就绪）后：`moment-api.ts` 新增 `reportByBiz`（统一举报，保留业务码）+ `REPORT_REASONS` 原因常量（对齐 B 站 10 项）；新建 `components/moment/ReportDialog.vue`（B 站风格：预设原因 radio + 其他输入 0/60 字数限制 + 图片附件 pics 最多 3 张，提交调 `reportByBiz`）；接入 5 个入口——动态 AllFeedView/TopicFeedView/MomentDetailView（bizType=dynamic）、评论 LotteryCommentItem（bizType=comment）、用户空间 MomentSpaceView（bizType=user）；替换散装 `ElMessageBox.prompt` 与占位；vue-tsc 类型检查通过、vite HMR 生效 |
| P11-T7 | 单元测试 | 完成 | 2026-08-15 | `tests/test_report.py`：`ReportBaseService.record_report` 幂等、三类举报分别写入 `TMomentReport`/`CommentReport`/`TUserReport`、pics/不存在动态校验、动态达阈值(3)联动转 `TMoment` auditing、管理端 list/review；8 passed |

### Phase 12（用户注销流程：彻底删除账号及其业务数据）

> **2.15.1 方案调整**：注销改为「分领域删除服务 + MQ 异步编排」，下表为调整后的任务划分。

| 任务 ID | 任务描述 | 状态 | 完成时间 | 说明 |
|---|---|---|---|---|
| P12-T1 | 注销消息载体 + 队列 | 完成 | 2026-08-15 | `models/schemas/mq.py` 新增 `UserDeactivatePayload(uid)`（已入 `schemas/__init__.py` 导入导出）；`core/broker.py` 新增 `RK_USER_DEACTIVATE` + `user_deactivate_queue`；`services/publisher.py` 新增 `publish_user_deactivate(uid)`（异常吞掉返回 bool） |
| P12-T2 | 领域删除服务拆分（cleanup_*） | 完成 | 2026-08-15 | 新建 `app/services/cleanup/` 包 + 10 个领域服务（cleanup_moment/comment/follow/report/favorite/dm/notify/event/misc/pptr），各自 `delete_all_by_uid(session, uid)`，逻辑自原 `UserDeactivateService` SQL 列表迁移；**顺带修复 pptr 表名未加双引号 bug**（`DELETE FROM TUserInfo` 在 Postgres 被折叠为小写 `tuserinfo` 报 `relation does not exist`，改为 `DELETE FROM "TUserInfo"`） |
| P12-T3 | `UserDeactivateService` 重构为编排器 | 完成 | 2026-08-15 | 降级为编排器：校验 uid + 按依赖顺序调用各 cleanup 服务（pptr 先、be-message 后，各自单事务），幂等（已注销/无数据仍正常返回） |
| P12-T4 | MQ 消费者（deactivate） | 完成 | 2026-08-15 | 新建 `app/consumers/deactivate.py`（`handle_user_deactivate`：成功 ack，失败记录日志后 ack 不 requeue）+ `app/mq/consumers/deactivate.py`（`@router.subscriber(user_deactivate_queue, MANUAL)`）；`app/mq/consumers/__init__.py` 与 `main.py` 均已 import 注册 |
| P12-T5 | 注销接口改投递 MQ | 完成 | 2026-08-15 | `/api/v1/user/deactivate`（本人，CurrentUser）与 `/api/v1/user/admin/deactivate`（AdminUser）改为只校验 uid + `publish_user_deactivate(uid)` 投递，返回「注销已提交」；OpenAPI 确认两接口已注册 |
| P12-T6 | JWT 失效 | 待开始 | | 复用 be-gateway Redis 签名黑名单撤销 token |
| P12-T7 | 前端注销入口 | 待开始 | | 用户设置页「注销账号」（二次确认 + 校验），调 `deactivateUser`；需前端 SDK 同步 |
| P12-T8 | 单元测试 | 完成 | 2026-08-15 | `tests/test_user_deactivate.py`（8 passed）：关注/动态+子表/杂项领域删除正确、全部 cleanup 服务空库安全执行（验证表名/字段 SQL 合法）、`deactivate` 编排（pptr 先 be-message 后）、重复注销幂等、uid 非法抛 ValueError、`publish_user_deactivate` 投递（monkeypatch broker 验证 payload/routing_key）；相关回归（space_info/report/comment_count_sync）13 passed |

### 内部维护（2.15.2：数据库全量重建 + seed 灌数提速）

| 任务 ID | 任务描述 | 状态 | 完成时间 | 说明 |
|---|---|---|---|---|
| M-T1 | 修复模型索引列名 bug | 完成 | 2026-08-16 | `moment.py`/`favorite.py` 的 DESC 索引 `text('"createdAt" DESC')`/`text('"pubTime" DESC')` 引用带双引号的不存在列（实际 `created_at`/`pubTime`），MySQL 双引号视为字符串字面量报语法错误；改为 `text('created_at DESC')`/`text('pubTime DESC')` |
| M-T2 | 数据库全量重建（清空 + autogenerate 单一 base） | 完成 | 2026-08-16 | 清空 BiliMessageDB 全部 30 表，`alembic revision --autogenerate -m base_rebuild_all_tables` 生成单一 base 迁移 `e9e8e4c7b148`（down_revision=None），`upgrade head` 一次建齐 30 表；TUserReport 等新表齐全，`dynId`/`created_at` 列存在，索引 9 个正确 |
| M-T3 | seed 脚本 executemany 批量提速 | 完成 | 2026-08-16 | `seed_moment_bulk.py` 主表/Stat/Like/ViewLog/AuditLog 全改 `session.execute(insert(Model), dict列表)` 多值 INSERT；话题阶段 `add_all` + 一次 flush（去逐行 flush）；删除未用 `_build_moment`；`insert` 入 import。10 万动态 + 27 万 Like + 83 万 ViewLog 2 分钟完成（原 ~20 分钟，~10 倍提速） |
| M-T4 | 对账脚本 bug 修复 + sys.path | 完成 | 2026-08-16 | `moment_stat.reconcile` 同 session 先跑聚合查询（返回 Row）后 `session.exec(select(Model))` 不再映射为模型实例（`AttributeError: likeCount`），改 `session.execute(...).scalars()`；`reconcile_moment_stats.py` 补 sys.path 注入支持直接运行。对账修正 likeCount 4971 / viewCount 10143，最终 `Stat.likeCount=TMomentLike=160869`、`Stat.viewCount=ViewLog=287888` 完全一致 |
| M-T5 | 测试清理 SQL 适配 ReportBase | 完成 | 2026-08-16 | `test_moment_interaction.py._cleanup` 的 `DELETE FROM TMomentReport WHERE dynId=` 引用已废弃 dynId 列（2.14.0 起为 ReportBase 结构），改 `bizType='dynamic' AND bizId=`；`test_report_writes_and_keeps_status` 断言 `TMomentReport.dynId` 改 `bizId` |

### Phase 13（用户头像修改：仅支持图片 URL + 后端下载校验）

| 任务 ID | 任务描述 | 状态 | 完成时间 | 说明 |
|---|---|---|---|---|
| P13-T1 | `PptrUserInfoUpdateParams` 新增 `avatar` 字段 | 完成 | 2026-08-16 | bili-common `avatar: str = Field(default="", max_length=1024)`（http/https，空串表示不修改）；OpenAPI 已含 avatar 字段 |
| P13-T2 | 头像 URL 下载校验服务 | 完成 | 2026-08-16 | 新建 `app/services/avatar_check.py`：httpx 流式下载，超时 1s（`AVATAR_DOWNLOAD_TIMEOUT=1.0`）、累计 ≤1MB（`AVATAR_MAX_BYTES`）、仅 http/https、Content-Type image/*；`verify_avatar_url(url, transport=)` 支持测试注入 MockTransport |
| P13-T3 | `/user_info/update` 接入 avatar 校验 + 落库 | 完成 | 2026-08-16 | `update_user_info` 非空 avatar 先 `verify_avatar_url`（失败 422 具体原因），通过后 `set_user_detail(face=avatar)` 落库；OpenAPI 确认接口与字段注册 |
| P13-T4 | 前端用户中心首页头像修改入口 | 完成 | 2026-08-16 | `UserCenterDefaultPanel.vue` 头像旁「修改头像」按钮 + el-dialog 弹窗（输入图片链接 + 1MB/1s 提示 + 错误展示）→ `UpdateUserInfo({ avatar })` → 刷新头像。**依赖 SDK 重新生成（PptrUserInfoUpdateParams 新增 avatar 字段，当前 types.gen.ts 未含，代码用 as any 规避）** |
| P13-T5 | 单元测试 | 完成 | 2026-08-16 | `tests/test_avatar_check.py`（7 passed）：非法协议/空 URL/非图 Content-Type/HTTP 错误/超 1MB/网络超时 拒绝 + 正常小图通过 |

### Phase 14（话题创建与审核，2.19.0，对齐动态发布审核流程）

> 03-phases.md 已有阶段计划（P14-T1~T10）；下表任务记录按 2.19.0 changelog 与代码现状补记。
> **编号说明**：03-phases 的 Phase 14/15 与 08-task-records 历史编号存在漂移（本文件原 Phase 14「抽奖卡片转发」已并入下方 Phase 15），此处以 03-phases 编号为准。

| 任务 ID | 任务描述 | 状态 | 完成时间 | 说明 |
|---|---|---|---|---|
| P14-T1 | 数据模型：`TMomentTopic` 新增审核字段 + `topicId` 改应用层雪花 ID | 完成 | 2026-08-16 | `auditStatus`(auditing/normal/rejected)/`auditRejectReason`/`pubTime`/`creatorMid`；`MomentTopicAuditStatusEnum`；alembic 迁移加列 + 索引；`topicId` 非自增，`generate_topic_id()`（bili-common `MinuteSnowflakeIdGenerator`，独立 `topic_id_worker_id`/`topic_id_epoch_sec`，新增规则 `snowflake-id.mdc`） |
| P14-T2 | 创建话题服务 + `POST /api/v1/moment/topic/create` | 完成 | 2026-08-16 | `MomentTopicService.create_topic`：名称唯一（重复 422）/长度/封面 URL/描述校验；创建即 auditing |
| P14-T3 | 管理端话题审核（`GET /list`、`POST /approve`、`POST /reject`，root 守卫） | 完成 | 2026-08-16 | `MomentTopicAuditService`：approve→normal+pubTime=now()（不发通知）；reject→rejected+原因（发驳回通知给创建者）；`app/api/moment_topic_audit.py` |
| P14-T4 | 对外可见性过滤（广场/热搜/Feed/详情仅 normal） | 完成 | 2026-08-16 | `/topic/square`、`/topic/hot-search`、话题 Feed 统一加 `auditStatus='normal'` 过滤；非 normal 话题对游客与作者均不可见 |
| P14-T5 | 发布动态关联话题校验（非 normal 拒绝） | 完成 | 2026-08-16 | 发布关联话题校验 `auditStatus='normal'`，否则 422（2.22.0 起改为 400，见 P18-T3） |
| P14-T6 | `GET /api/v1/moment/topic/mine`（我创建的话题及审核状态） | 完成 | 2026-08-16 | `CurrentUser`，按 createdAt 倒序，返回 auditStatus/auditRejectReason/pubTime |
| P14-T7 | 前端话题广场创建入口 + 我的话题展示 | 完成 | 2026-08-16 | `TopicSquareView`「创建话题」按钮（弹窗 → `createTopic` → 「已提交审核」）；「我创建的话题」展示审核状态徽标 |
| P14-T8 | 前端管理后台「话题审核」页 | 完成 | 2026-08-16 | `AdminLayout` 入口 + `TopicAuditListView.vue`（列表 + 通过/驳回弹窗），路由 `ADMIN_MOMENT_TOPIC_AUDIT` |
| P14-T9 | SDK 更新后前端接入（`createTopic`/`fetchMyTopics`/`fetchTopicAuditList` 等） | 完成 | 2026-08-16 | moment-api.ts 薄封装；hey-api SDK 经用户重新生成后接入 |
| P14-T10 | 单元测试 | 待开始 | 2026-08-16 | 03-phases 未勾选；话题创建/审核/过滤链路已在 2.19.0 API 验证覆盖，独立单测待补 |

### Phase 15（抽奖卡片互动补全：lottery 点赞/收藏/转发到动态，2.20.0）

> 03-phases.md 已有阶段计划；下表任务记录按 2.20.0 changelog 与代码现状补记。

| 任务 ID | 任务描述 | 状态 | 完成时间 | 说明 |
|---|---|---|---|---|
| P15-T1 | be-bilibili-crawler 提供 lottery 校验 RPC（`check_lottery_exist`，契约并入 bili-common） | 完成 | 2026-08-16 | `LotteryRpcClient` 弱依赖 RPC（经 RabbitMQ 调 be-bilibili-crawler），超时/未连接/不存在返回 False |
| P15-T2 | be-message `LotteryRpcClient`（`lottery_exists`） | 完成 | 2026-08-16 | `app/services/lottery_rpc.py`，参照 `rpa_rpc.py` 弱依赖降级 |
| P15-T3 | 注册 lottery 校验器（点赞/收藏/转发 RESOURCE=lottery 校验存在，不存在 422） | 完成 | 2026-08-16 | `InteractionResourceValidator.register(InteractionBizTypeEnum.LOTTERY, checker)`；`moment_interaction.thumb`/`favorite`/`moment_publish` 校验接入 |
| P15-T4 | 动态转发 attach lottery（复用 create RESOURCE 节点） | 完成 | 2026-08-16 | 前端 `LotteryForwardDialog.vue` 转发抽奖走 `POST /moment/create`（`scene='WORD'`，不再用非法的 `'PUBLIC'`），`content` 组装 `[WORDS, RESOURCE]`，RESOURCE 仅传 `bizType/bizId/name`（不存 cover/jumpUrl 快照）；后端 `_validate_resource_nodes` 经 `InteractionResourceValidator` 校验存在（不存在 422）；覆盖入口 `BiliLotteryCard`/`BiliLotterySimpleList`/`BiliOfficialLotteryTable` 三点菜单（2.20.0 后并入统一编辑器，见 P16-T2） |
| P15-T5 | 前端 `BiliLotteryCard` 点赞/收藏/转发按钮（列表+详情） | 完成 | 2026-08-16 | 点赞 `POST /moment/thumb`、收藏 `POST /favorite/add|remove`（bizType=lottery）；互动状态由容器层批量 `GET /moment/interaction-status` 下发，卡片 `emit('update-status')` 上报；moment-api.ts 封装 `thumbMoment`/`favoriteAdd`/`favoriteRemove`/`fetchInteractionStatus` |
| P15-T6 | 前端动态 Feed 渲染 lottery 卡片（RESOURCE 渲染 + 跳转抽奖详情） | 完成 | 2026-08-16 | `MomentContentRenderer` RESOURCE=lottery 渲染 + 跳转；卡片展示点赞/收藏状态与计数 |
| P15-T7 | 单元测试（lottery 校验器/点赞收藏校验/转发校验/前端交互） | 进行中 | 2026-08-16 | 03-phases 未勾选；lottery RPC 校验与互动链路已在 2.20.0 API 验证中覆盖，独立单测待补 |
| P15-T8 | 读取动态时 RPC 批量获取 lottery 详情（替代快照） | 完成 | 2026-08-16 | `CheckLotteryExistRpcResult` 扩展 title/cover/jumpUrl（be-bilibili-crawler 从 `Lotdata` 回填）；`get_lottery_details` 批量一次 RPC；Feed 装配先收集本页 lottery_id 去重批量回查再分发（弱依赖降级保留原节点）；前端 `LotteryForwardDialog` 只传 bizType/bizId/name |

### Phase 16（统一动态编辑器：转发/创建共用最全编辑器）

| 任务 ID | 任务描述 | 状态 | 完成时间 | 说明 |
|---|---|---|---|---|
| P16-T1 | 统一创建/转发动态编辑器 | 完成 | 2026-08-17 | 将 `MomentRepostDialog`（仅 textarea）合并进 `MomentPublishForm`（功能最全：`el-mention`@用户 + 图片 URL 列表 + 话题选择 + 编辑模式），新增**转发模式**（`isRepost` + 原动态预览块 + `srcDynId`），复用同一套编辑器避免重复。抽取「纯文本→富文本节点」为共享工具 `src/utils/momentContent.ts`（WORDS/AT/TOPIC + 图片 LINK 节点），供发布/编辑/转发共用；`MomentPublishForm` 转发模式内部调 `repostMoment` 并 emit success；`MomentRepostDialog.vue` **已删除**（代码验证），`MomentCard`/`MomentDetailView` 改用转发模式。后端 `repost` 已支持任意 content 节点（AT/TOPIC/LINK），无需改动 |
| P16-T2 | 抽奖卡片转发并入统一编辑器 | 完成 | 2026-08-17 | `MomentPublishForm` 新增 `attachResource`（`{bizType,bizId,name,cover}`）能力：发布模式传入时底部渲染 attach 卡片预览（对标 B 站贴卡），提交时内部 `createMoment`（scene=WORD）并在 content 节点末尾追加 `RESOURCE` 节点、emit success。`buildMomentContentNodes` 增加 `attachResource` 参数统一追加 RESOURCE。`LotteryForwardDialog.vue` **已删除**（代码验证），`BiliLotteryCard`/`BiliLotterySimpleList`/`BiliOfficialLotteryTable` 三点菜单「转发到动态」改用 `MomentPublishForm`（attachResource=lottery） |

### Phase 17（attach 卡片重构：独立 module + 只存 bizId/bizType + RPC 实时详情）

| 任务 ID | 任务描述 | 状态 | 完成时间 | 说明 |
|---|---|---|---|---|
| P17-T1 | attach 卡存储迁移到 `TMoment.bizType/bizRid` | 完成 | 2026-08-17 | 2.21.0 起 attach 卡**不再写入正文 `contentJson` RESOURCE 节点**，改存复用 `TMoment.bizType`（VARCHAR 32）+ `TMoment.bizRid`（BIGINT）两列，**只落 bizType+bizId**（不冗余存 name/cover/jumpUrl 快照，无表结构变更）。`MomentCreateReq`/`MomentEditReq` 新增可选 `attach: {bizType,bizId}`（对标 B 站 `CreateCommonAttachCard {type, biz_id}`）；`_resolve_attach` 解析 attach，旧客户端正文 RESOURCE 节点兼容提升为 attach 并剥离；发布/编辑/转发经 `_validate_attach`（`InteractionResourceValidator`）校验资源存在（不存在 422） |
| P17-T2 | Feed/详情装配独立 `moduleType="additional"` 模块 | 完成 | 2026-08-17 | `_build_feed_item` 在 desc 模块之后追加 `moduleType="additional"` 模块（对标 B 站 `DynModuleType.module_additional`），渲染于正文下方；只带 `bizType`+`bizId`（字符串），lottery 经 `LotteryRpcClient.get_lottery_details` 批量 RPC 实时填充 name/cover/jumpUrl（弱依赖失败仅返回 bizType+bizId）。`_collect_lottery_ids` 改为从 `TMoment.bizType/bizRid` 收集（兼容旧数据回扫 contentJson RESOURCE=lottery 节点）。`MomentModule` 增加 additional 模块字段（bizType/bizId/name/cover/jumpUrl） |
| P17-T3 | 开发规范：参考 bilibili proto 设计 | 完成 | 2026-08-17 | 新增规则 `.codebuddy/rules/bilibili-proto-design.mdc`：设计社区互动接口/模型时参考 `be-bilibili-crawler/Service/GrpcModule/Grpc/GrpcProto/bilibili` 目录下的 proto 定义（`dynamic/common/dynamic.proto` 通用模型、`dynamic/gw/gateway.proto` 网关模型、`dynamic/interfaces/feed/v1/api.proto` Feed 接口、`app/dynamic/v2/dynamic.proto` V2 模块）；模块化渲染对齐 `DynModuleType`（附加卡为独立 `module_additional` 模块）；attach 卡只存 bizType+bizId，读取时 RPC 实时获取详情 |
| P17-T4 | 前端接入（依赖 SDK 重新生成） | 完成 | 2026-08-17 | ① `moment-api.ts` 封装 `MomentCreateReq.attach`（attachResource 单独提交，不再追加 RESOURCE 节点）；② `MomentCard`/`MomentDetailView` 按 `moduleType="additional"` 在正文下方渲染附加卡（新增 `MomentAttachCard.vue`）；③ `MomentContentRenderer` 移除 RESOURCE 内联渲染（旧数据兼容保留）。hey-api SDK 已由用户重新生成（含 attach/additional 字段）后接入；浏览器实测 additional 模块（lottery 详情）正常显示 |

### Phase 18（2.22.0 动态话题多选改造：正文禁话题 + 多话题 + 上限 5）

| 任务 ID | 任务描述 | 状态 | 完成时间 | 说明 |
|---|---|---|---|---|
| P18-T1 | 新增 `TMomentTopicRel` 多对多关系表 | 完成 | 2026-08-17 | `app/models/db/moment.py` 新增 `TMomentTopicRel`（pk BIGINT 自增内部主键 + `dynId` FK→TMoment（CASCADE）+ `topicId` BIGINT 仅存 ID + `UniqueConstraint(dynId,topicId)` + `idx_topic_rel_topic(topicId,dynId)`），追加 `__all__` 与 `db/__init__.py` 导出；Alembic 迁移 `20260817_2230-f3a5b9c7d1e2_add_tmoment_topic_rel.py`（down=e11993cc8480）已执行，表结构与索引/FK 验证无误 |
| P18-T2 | `MomentCreateReq`/`MomentEditReq` 新增 `topics` 多话题字段（兼容保留 `topic`） | 完成 | 2026-08-17 | schemas/moment.py：`topics: list[MomentTopicRef] | None`（2.22.0），兼容保留 `topic: MomentTopicRef | None`；`MomentModule` 新增 `topics: list[MomentTopicRef] | None`（保留 topicId/topicName=主话题） |
| P18-T3 | 发布/编辑多话题校验与落库（上限 5、去重、仅 normal） | 完成 | 2026-08-17 | `_resolve_topics`（合并 `topics`+`topic` 去重、数量≤`_MAX_TOPIC_COUNT`=5）、`_validate_topics`（一次 IN 校验存在且 `auditStatus='normal'`，否则 400——路由层统一 ValueError→400）、`_persist_topic_rels`（批量写关系表）；`_create_word`/`_create_forward` 主话题写 `TMoment.topicId`=topics[0]、全部写 `TMomentTopicRel`（同事务）；`edit` 先 `DELETE` 旧关系再重建。**API 回归验证**：传 3 个话题（含重复）→ 合并去重 1 个、bizType/bizRid 正常落库、关系表仅 1 行；未过审话题 create 返回 400「话题不存在或未通过审核」；编辑 topics=[] → `TMoment.topicId=None` 且关系表清空 |
| P18-T4 | Feed/详情 extend 模块返回多话题 `topics` | 完成 | 2026-08-17 | 新增 `_load_topic_rel_map`（批量 IN 查询按 dynId 分组）；`_build_feed_item` extend 模块输出 `topicId`(主话题)+`topics[]`（关系表+主话题列并集，主话题排首位）；`_attach_topics` 批量回填 `topics` 数组的 topicName（含嵌套 srcMoment 递归补齐）；`topic_feed` 双条件查询（`TMoment.topicId=? OR dynId IN(关系表)`）。**临时脚本验证**：2 话题动态装配 `extend.topics=[(甲,话题甲),(乙,话题乙)]` 正确回填名称 |
| P18-T5 | 前端：发布表单多话题选择 + 正文禁 `#话题#` | 完成 | 2026-08-17 | `momentContent.ts` 已移除 `#话题#`→TOPIC 解析（正文不再解析话题，`#话题#` 按普通文本）；`MomentPublishForm` 话题选择器改 `el-select multiple`（`multiple-limit=5`、collapse-tags）、placeholder/hint 移除「#话题#」提示；emit payload 与 attach 模式 body 均改提交 `topics[]`；`MomentSpaceView`/`MomentLayout` 的 `handlePublish` 同步改 `body.topics`（SDK 已由用户重新生成，含 `MomentCreateReq.topics`/`MomentEditReq.topics`） |
| P18-T6 | 前端：卡片/详情多话题卡渲染（作者信息与正文之间） | 完成 | 2026-08-17 | `MomentCard` 新增 `topicModules` computed（`extend.topics[]` 优先，无则单话题 topicId 兜底），话题区改为 `topics[]` 循环逐卡渲染（作者信息下方/正文上方），`openTopic` 接受话题参数逐卡跳转；`MomentContentRenderer` 保留存量 TOPIC 节点兼容渲染（SDK 已更新，含 `MomentModule.topics`） |
| P18-T7 | 验证与回归 | 完成 | 2026-08-17 | 后端 API 级回归全部通过：attach 落库（lottery/413014）、多话题去重落库、Feed 装配 `additional` 模块（RPC 实时取 lottery 详情）+ `extend.topics` 多话题回填、未过审话题 400、编辑重建关系（topics=[] → 主话题清空+关系表清空）。**端到端验证通过**：发布(attach+2话题) → root 审核 → space feed 完整呈现 additional+extend.topics → 话题 Feed 按非主话题(t2) 经关系表命中。前端 lint 0 诊断；`vue-tsc --build` 失败为项目原有 tsconfig `baseUrl` 与 TS6 兼容问题（pre-existing，与本次无关）。**待用户在前端 UI 实测**（dev server 5173 已热更新） |

### Phase 19（2.22.1 管理员删除动态：管理端动态治理）

| 任务 ID | 任务描述 | 状态 | 完成时间 | 说明 |
|---|---|---|---|---|
| P19-T1 | 计划书更新 | 完成 | 2026-08-17 | README changelog 新增 2.22.1；05-api 新增 `POST /moment/admin/remove` 说明（RootUser，任意动态，operatorRole=admin） |
| P19-T2 | 后端：`remove` 服务支持管理员操作 | 完成 | 2026-08-17 | `MomentPublishService.remove` 新增 `operator_role="owner"|"admin"`：admin 跳过 `dyn.mid != mid` 校验可删任意动态；软删语义/状态机触发点④（FORWARD ∧ before=normal → 源 repostCount -1）/幂等（已删直接返回）不变；`_build_audit_log` 新增 `operator_role` 参数（默认 author），管理员删除写 `operatorRole=admin` + `remark='管理员删除'` |
| P19-T3 | 后端：新增 `POST /api/v1/moment/admin/remove` 路由 | 完成 | 2026-08-17 | `app/api/moment.py` 新增，`RootUser` 权限（与 `moment/audit/*` 一致），复用 `MomentRemoveReq`（body 只需 `dynId` int）/`MomentRemoveResp`，ValueError→400 |
| P19-T4 | 前端：`moment-api.ts` 封装 `adminRemoveMoment` | 完成 | 2026-08-17 | 薄封装 `adminRemoveDynamicApiV1MomentAdminRemovePost`，入参 dynIdStr（SDK 已由用户重新生成） |
| P19-T5 | 前端：删除入口下沉到复用组件 `MomentCard`（2.22.1 修订：不再局限于审核页） | 完成 | 2026-08-17 | `MomentCard`：More 菜单删除项显示条件改为 `effectiveCanRemove ∨ is_root`（管理员对任意动态可见）；`handleRemove` 统一托管确认+删除（管理员 → `adminRemoveMoment` 文案「管理员删除动态？」；作者 → `removeMoment` 文案不变），成功 emit `remove`；`onMounted` 防御性加载 admin status。`useRpaAdminStore.fetchStatus` 增加 in-flight 去重（避免列表多卡片并发重复请求）。父组件 4 处 `handleRemove`（AllFeedView/TopicFeedView/MomentSpaceView/MomentDetailView）改为仅移除列表项/详情返回（确认与 API 调用已下沉卡片）。审核页 `MomentAuditListView` 不再新增删除按钮（非卡片场景）。**2.22.2 bugfix**：`effectiveCanRemove` 由 `props.canRemove !== undefined` 改为 `if (props.canRemove) return true`——Vue3 Boolean props 未传入默认 `false` 而非 `undefined`，原判断恒成立导致未传 `can-remove` 的场景作者删除入口永不显示；浏览器实测综合 Feed/空间页/详情页三场景删除入口均正常显示；lint 0 诊断 |
| P19-T6 | 验证与回归 | 完成 | 2026-08-17 | **API 级验证通过**：normal 调 admin/remove → 403「仅管理员可执行该操作」；root 删他人 normal 转发动态 → success；状态机④ 源动态 repostCount 1→0 正确回退；已删幂等重删 success；AuditLog `operatorRole=admin` + `remark='管理员删除'`。前端 lint 0 诊断，待用户 UI 实测 |

### Phase 20（2.23.0 前端下线动态编辑 + 审核状态标记修复）

| 任务 ID | 任务描述 | 状态 | 完成时间 | 说明 |
|---|---|---|---|---|
| P20-T1 | 计划书更新 | 完成 | 2026-08-17 | README changelog 新增 2.23.0；编辑功能下线方案：仅前端入口移除，后端 `/edit` 接口与状态机触发点③、SDK 契约保留 |
| P20-T2 | 前端移除编辑入口 | 完成 | 2026-08-17 | `MomentCard.vue` 移除 `canEdit` prop、「编辑」菜单项（`moment-card__action-edit`）、`handleEdit`、`Edit` 图标 import；`MomentSpaceView.vue` 移除 `:can-edit="isOwnSpace"`、`@edit="handleEdit(item)"`、`handleEdit` 函数（原实现「编辑功能开发中」占位）；`MomentDetailView` 无编辑入口不受影响；`MomentPublishForm.isEdit` 模式保留（从未被传入，表单通用能力，后端 `/edit` 接口保留） |
| P20-T3 | 修复 `auditBadge` 审核状态标记 | 完成 | 2026-08-17 | `MomentCard.auditBadge` 由 `switch(auditStatus){case -1/0/1}`（数字，与后端 StrEnum 字符串不匹配，标记从未显示）改为字符串枚举匹配：`'auditing'`→`待审核`(warning)、`'rejected'`→`审核驳回`(danger)、`'hidden'`→`已下架`(info)、其余→null；作者本人空间/详情页对非审核通过动态显示状态标签 |
| P20-T4 | 验证与回归 | 完成 | 2026-08-17 | **浏览器实测（mock 登录态+接口）通过**：综合 Feed 中 auditing 动态显示「待审核」标签；详情页 rejected 动态显示「审核驳回」标签；卡片 More 菜单仅「删除 + 举报」（无「编辑」）；删除/举报入口正常；lint 0 诊断 |

### Phase 21（2.24.0 用户空间 Tab 路由化：主页/动态/合集/收藏/设置 → router 子页面）

| 任务 ID | 任务描述 | 状态 | 完成时间 | 说明 |
|---|---|---|---|---|
| P21-T1 | 计划书更新 | 完成 | 2026-08-17 | README changelog 新增 2.24.0（纯前端 Tab 改下拉菜单；初始方案「路由化」经用户反馈改为「菜单」，路由改动已回滚） |
| P21-T2 | Tab 栏改为下拉菜单 | 完成 | 2026-08-17 | `MomentSpaceView` 模板：移除原横排 `moment-space__tabs` Tab 循环，改为「空间」label + `el-dropdown`：trigger 显示 `currentTab`（图标+标题+`ArrowDown`），下拉菜单循环 `tabs`（当前项高亮），`@command="switchTab"` |
| P21-T3 | script 调整 | 完成 | 2026-08-17 | 新增 `currentTab` computed（含图标，回退 dynamic）与 `switchTab(name)`；移除 `currentTabTitle`（`EmptyState` 改 `currentTab.title`）；补 `ArrowDown`、移除已下线 `Edit` 图标 import；**路由不变**（`/app/space`、`/app/space/:mid`，`activeTab` 保持 ref） |
| P21-T4 | 验证与回归 | 完成 | 2026-08-17 | **浏览器实测通过**：空间页显示「空间 ▾ 主页」菜单 trigger；点击展开 5 项（主页/动态/合集和系列/收藏/设置，精确匹配 `moment-space__tabs-menu`）；点击「设置」trigger 变「设置」、URL 保持 `/app/space`（页面内切换）；lint 0 诊断 |

### Phase 22（2.25.0 用户空间 UI 整改：横向导航栏 + 登录态 bugfix + B 站 SVG 接入）

| 任务 ID | 任务描述 | 状态 | 完成时间 | 说明 |
|---|---|---|---|---|
| P22-T1 | 计划书更新 | 完成 | 2026-08-18 | README changelog 新增 2.25.0 |
| P22-T2 | 「请先登录」bugfix | 完成 | 2026-08-18 | 根因：登录态异步加载（App.vue onMounted 后才请求 nav），空间页 setup 同步读 `user_nav.uid` 恒空 → 自己空间误判未登录。修复：`routeMid`/`currentMid` 改响应式 `computed`；`activeTab` 默认值改 `resolvedActiveTab` computed 兜底（模板 6 处引用同步替换）；`onMounted` 增加「自己空间且 uid 未就绪 → watch uid 到达后再 loadFirst」（避免误报「请先登录」） |
| P22-T3 | 头像上移 | 完成 | 2026-08-18 | `-mt-12` → `-mt-16`（更贴近横幅，对标 B 站） |
| P22-T4 | B 站 SVG 资源整理 | 完成 | 2026-08-18 | `font_2_svg/`（B 站逆向抓取，无源文件）不作为正式资源：94 个图标经脚本**转换规范格式**（`width="100%" height="100%" fill="currentColor"`、path 去 fill）并**平铺到 `src/assets/svgs/space/`**（空间页专用，简化语义命名；`tag`/`tag_alt` 冲突显式区分；多色徽章 `user_level_0~6(.color0/1)`；映射表 `space/_svg_map.md`）；逆向目录 `font_2_svg/` 已删除；`space/` 原 5 个手写导航图标由 B 站版替换 |
| P22-T5 | 导航改横向栏 | 完成 | 2026-08-18 | 移除 2.24.0 下拉菜单（`el-dropdown`），改为 B 站风格横向导航栏：`ownTabs`/`otherTabs`/`tabs` 的 `icon` 由 Element 图标换为 SVG 组件（`?component` import）；模板 `moment-space__tab-item` 横排渲染（`flex items-center gap-1.5 px-4 py-2`，激活项 `text-msg-link bg-primary-light-3/50 font-bold`，`@click="switchTab"`）；移除未用 Element 图标 import（ArrowDown/HomeFilled/Share/Star/Setting）；**根目录原 `like.svg`/`setting.svg`/`more.svg` 被同名覆盖，已用 `space/` 下语义一致 B 站规范版恢复** |
| P22-T6 | 验证与回归 | 完成 | 2026-08-18 | lint 0 诊断；vite 编译产物含新导航栏结构、SVG 资源 200 可访问；**2.25.1 bugfix**：vite-svg-loader 默认 svgo 3.x 优化处理 B 站 `q` 曲线路径崩溃（reflectPoint undefined），`vite.config.ts` 改 `svgLoader({ svgo: false })` 禁用优化，清缓存后首次编译 5 导航图标全部 200 无报错；agent-browser 环境不稳定未完成完整 UI 实测，待用户浏览器确认（横向导航渲染 + 激活高亮 + Tab 切换） |

### Phase 23（2.26.0 空间页用户等级徽章）

| 任务 ID | 任务描述 | 状态 | 完成时间 | 说明 |
|---|---|---|---|---|
| P23-T1 | 计划书更新 | 完成 | 2026-08-18 | README changelog 新增 2.26.0 |
| P23-T2 | 等级徽章 SVG 接入 | 完成 | 2026-08-18 | `space/user_level_0~6.svg`（B 站风格单色完整版，规范格式 `width="100%" height="100%" fill="currentColor"`）`?component` import；`LEVEL_BADGES` 数组（Lv0~Lv6） |
| P23-T3 | 模板渲染 + computed | 完成 | 2026-08-18 | `levelBadge` computed：`targetUser.level`（0~6）动态选图，越界/未知返回 undefined；模板用户名右侧 `<component :is="levelBadge" class="w-6 h-6">` + `title="等级 Lv.N"`；数据复用后端 `space/info` 的 `SpaceInfoResp.level`（`TUserLevel.current_level`） |
| P23-T4 | 验证与回归 | 完成 | 2026-08-18 | 浏览器实测（mock level=5）：徽章渲染 1 个 SVG、`title="等级 Lv.5"`；lint 0 诊断；7 个徽章 SVG vite 编译全部 200 |

### Phase 24（2.28.0 收藏夹封面「大小控制 + 先审后发」：对齐头像审核模式）

| 任务 ID | 任务描述 | 状态 | 完成时间 | 说明 |
|---|---|---|---|---|
| P24-T1 | 计划书更新 | 完成 | 2026-08-21 | README changelog 新增 2.28.0；07-decisions 新增决策 #31（收藏夹封面大小控制 + 先审后发）；04-database 新增 §4.8.1 `TFolderCoverAudit`；05-api 收藏夹接口语义更新 + 封面审核接口；03-phases 新增 Phase 24 |
| P24-T2 | 数据模型 + 枚举 + 迁移 | 完成 | 2026-08-21 | 新增 `app/models/db/folder_cover_audit.py`（`TFolderCoverAudit`：folderId/mid/oldCover/newCover/auditStatus/auditOperatorMid/auditReason/auditedAt + `idx_folder_cover_audit_status_created`/`idx_folder_cover_audit_folder_status`）+ `FolderCoverAuditStatusEnum`（pending/approved/rejected）+ Alembic 迁移 `20260821_1400-a2b4c6d8e0f2_add_tfolder_cover_audit.py`（down=933be090d6a9，`alembic upgrade head` 已执行，表结构与索引验证无误） |
| P24-T3 | 封面校验复用 + 收藏夹服务改造 | 完成 | 2026-08-21 | `avatar_check.verify_avatar_url` 新增 `label` 参数（默认"头像"）；`FavoriteService.create_folder`/`update_folder` 非空 `coverUrl` 先下载校验（失败 ValueError，路由层 422）再经 `FolderCoverAuditService.submit` 进 pending（不写 cover_url），返回 `coverAuditStatus`；空串清除封面直接清 cover_url；`list_folders` 批量一次 IN 回填各夹 `coverAuditStatus`（pending 标记）；`FavoriteFolderResp` 新增 `coverAuditStatus` 字段 |
| P24-T4 | 审核服务 + 管理端接口 + 用户侧 mine | 完成 | 2026-08-21 | `app/services/folder_cover_audit.py`（`FolderCoverAuditService`：submit 同夹 pending 覆盖/pending_list/approve 同事务写 cover_url + 通知/reject 保持原封面 + 原因 + 通知/mine）+ `app/api/folder_cover_audit.py`（前缀 `/api/v1/favorite/folder/cover/audit`：`GET /list`/`POST /approve`/`POST /reject` RootUser + `GET /mine` CurrentUser），`main.py` 注册；`app/api/favorite.py` create/update 路由适配（ValueError→422，收藏夹不存在→400） |
| P24-T5 | 清理 + 单测 | 完成 | 2026-08-21 | `cleanup_favorite` 注销清理同步删除 `TFolderCoverAudit`；新增 `tests/test_folder_cover_audit.py` 13 项全通过（创建/更新封面进 pending、封面不落库、旧 pending 覆盖、校验失败抛错且无残留、空串清除、approve 写 cover_url + 通知、reject 保持原封面 + 原因 + 通知、mine、pending 列表仅 pending、重复审核拒绝）；头像既有测试 17 项回归通过 |

### Phase 24.1（2.28.1 PATCH 图片 URL 校验增加文件后缀名判断：防攻击）

| 任务 ID | 任务描述 | 状态 | 完成时间 | 说明 |
|---|---|---|---|---|
| P24.1-T1 | 计划书更新 | 完成 | 2026-08-21 | README changelog 新增 2.28.1；版本号 PATCH 0→1；决策 #31 校验规则补充「文件后缀名白名单」 |
| P24.1-T2 | 后缀名校验实现 | 完成 | 2026-08-21 | `avatar_check.verify_avatar_url` 在协议校验后、下载前新增后缀名校验：`urllib.parse.urlparse` 提取路径（忽略 query）取后缀小写，必须命中白名单 `{".jpg",".jpeg",".png",".gif",".webp",".bmp",".avif"}`，否则拒绝（不发起网络请求）；头像/收藏夹封面共用一处加固 |
| P24.1-T3 | 单测补充 | 完成 | 2026-08-21 | `tests/test_avatar_check.py` 新增：非法后缀 `.php`/`.svg` 拒绝、无后缀 URL 拒绝、合法 `.jpg`/`.png`（含 query 参数）放行；既有用例全部回归通过 |

### Phase 24.2（2.28.2 雪花 ID 生成器统一收敛：内部实现重构）

| 任务 ID | 任务描述 | 状态 | 完成时间 | 说明 |
|---|---|---|---|---|
| P24.2-T1 | 计划书更新 | 完成 | 2026-08-21 | README changelog 新增 2.28.2（PATCH）；07-decisions 新增决策 #32（生成器统一收敛 + 锁外等待）；03-phases 新增 Phase 25 |
| P24.2-T2 | bili-common 通用生成器 | 完成 | 2026-08-21 | `SnowflakeIdGenerator`（可配 timestamp_bits/worker_bits/sequence_bits/time_unit，序列耗尽锁外等待）；`MinuteSnowflakeIdGenerator` 改为兼容特化（签名与位布局不变） |
| P24.2-T3 | sharding.py 收敛 msgkey | 完成 | 2026-08-21 | 毫秒级 msgkey 改用通用类（41+10+12 / millisecond），删除 `MsgKeyGenerator` 重复实现；`parse_timestamp_ms` 改用生成器 `timestamp_shift` |
| P24.2-T4 | 回归验证 | 完成 | 2026-08-21 | 分钟级/毫秒级 ID 数值与位布局不变（首 ID 与旧公式一致、位布局精确匹配）、序列耗尽锁外等待不持锁（17 个 ID 跨分钟等待 60s 正常）、并发 8 线程 8000 ID 唯一、49 个既有单测全通过 |

### Phase 26（2.29.0 全互动种子脚本：纯 HTTP 接口版，覆盖动态/评论/收藏/关注/事件/通知/私信/举报/封禁/审核流）

| 任务 ID | 任务描述 | 状态 | 完成时间 | 说明 |
|---|---|---|---|---|
| P26-T1 | SeedClient 全互动动作封装 | 完成 | 2026-08-21 | 在既有 `SeedClient`（`seed_moment_via_api.py`）基础上补全：话题 create/approve、动态 report/top、评论 add(root=0)/楼中楼(root,parent)/action/report/top、收藏夹 folder create/add/setting/cover-approve、关注 do/block、事件 event/report（like/reply/at）、系统通知 notify/admin/create、私信 dm/send/admin-audit/ack、用户举报 /report(bizType=user)、封禁 admin/ban、头像 submit/approve、lottery 点赞（TInteractionStat 通用计数） |
| P26-T2 | 四大模块编排 | 完成 | 2026-08-21 | `seed_moment`（动态体系）/ `seed_comment`（评论体系）/ `seed_interact`（用户级互动）/ `seed_message`（消息与管理）四大模块按依赖顺序编排；`_fallback_normal_ids` 兜底（跳过动态模块时从 Feed 拉已过审动态） |
| P26-T3 | 参数与开关 | 完成 | 2026-08-21 | `--base-url`（默认 http://127.0.0.1:18739）/ `--admin-mid`（默认 11）/ `--count`（动态条数，默认 50000）/ `--users`（真实用户数，默认 5000）/ `--concurrency`（并发，默认 20）；分模块开关 `--skip-moment`/`--skip-comment`/`--skip-interact`/`--skip-message`/`--skip-follow`；`--dry-run` |
| P26-T4 | 运行验证 | 进行中 | 2026-08-21 | 需本地启动 be-message-service 后运行 `uv run python scripts/seed_cli.py` 一个命令全量验证（依赖运行环境就绪） |
| P26-T5 | 单文件合并（全互动 + 大数据灌数） | 完成 | 2026-08-22 | `seed_via_api.py`（全互动）与 `seed_moment_bulk.py`（大数据灌数）合并入单文件 `scripts/seed_cli.py`（`SeedClient`/`seed()`/`run_bulk()` 同文件、不跨模块引用），删除冗余脚本；分模块开关控制分开执行 |
| P26-T6 | 统一大规模灌数改造 | 进行中 | 2026-08-22 | **取消「小规模联调 + 大数据灌数」两阶段**，合并为统一大规模灌数流程：动态/评论/互动/消息四模块全部大批量灌入（默认动态 5 万、其余按比例放大）；动态/话题取 biliopusdb 真实数据，用户统一取 pptr Postgres；大规模单条失败软降级跳过，私信撤回/删除全流程断言保留响亮报错 |

### Phase 27（2.30.0 管理端审核列表按状态筛选：可驳回已过审动态）

| 任务 ID | 任务描述 | 状态 | 完成时间 | 说明 |
|---|---|---|---|---|
| P27-T1 | 计划书更新 | 完成 | 2026-08-22 | README changelog 新增 2.30.0（MINOR）；05-api 5.5 节 `GET /list` 新增 `auditStatus` 参数说明 + `POST /reject` 补充「支持从任意状态驳回」；03-phases 新增 Phase 27 |
| P27-T2 | 后端接口按状态筛选 | 完成 | 2026-08-22 | `app/api/moment_audit.py` `audit_list` 新增 `auditStatus` query 参数（`MomentAuditStatusEnum` 枚举类型，默认 auditing，非法值 FastAPI 自动 422）；`MomentAuditService.pending_list` 增加 `audit_status` 参数按状态过滤（默认 auditing，向后兼容）；`uv run python -c "import app.api.moment_audit"` 通过 |
| P27-T3 | 单元测试 | 完成 | 2026-08-22 | `tests/test_moment_audit.py` 新增 2 用例：`test_pending_list_filters_by_status`（auditing/normal/rejected 三状态筛选互不串扰）、`test_reject_normal_word_moment_reverts`（normal 动态驳回撤回 → rejected + 原因 + 从 normal 列表消失进入 rejected 列表）；全文件 12 用例全部通过（41.28s） |
| P27-T4 | 前端接入 | 完成 | 2026-08-22 | SDK 已由用户重新生成（`MomentAuditStatusEnum` + `audit/list` query 参数就绪）后接入：`moment-api.ts` `fetchAuditList` 透传 `auditStatus`（re-export `MomentAuditStatusEnum`）；`MomentAuditListView.vue` 新增状态 Tab（待审核/已过审/已驳回，`el-radio-group`，切换重置页码）+ 状态列按实际 `auditStatus` 渲染标签（auditing=待审核 warning / normal=已过审 success / rejected=已驳回 danger / hidden=已下架 info）+ 操作列按状态区分（auditing=通过+驳回 / normal=仅驳回 / rejected=仅通过）+ 空态文案按状态；vue-tsc 对本次改动文件 0 错误。**注**：type-check 暴露一批**既有**类型错误（moment-api.ts FolderCoverAudit* 类型未导入 import 块、`InteractionBizTypeEnum` SDK 不再导出、`MomentLikerListResp` 等 re-export 缺失，均属 P24 遗留 + SDK 更新，非本次引入） |

### Phase 28（2.31.0 雪花 ID 序列号位宽可配：开发/测试灌数扩容）

| 任务 ID | 任务描述 | 状态 | 完成时间 | 说明 |
|---|---|---|---|---|
| P28-T1 | 计划书/规则更新 | 完成 | 2026-08-22 | 规则 `snowflake-id.mdc` 第 3 条位布局改为「总位数恒 39 bits，sequence_bits 默认 4、环境变量可配（范围 4~15），变更位宽会与已发布 ID 重叠、仅限清库重建环境」；README changelog 新增 2.31.0（MINOR）；03-phases 新增 Phase 28；07-decisions 新增决策 #33 |
| P28-T2 | bili-common 生成器支持 sequence_bits | 进行中 | 2026-08-22 | `MinuteSnowflakeIdGenerator` 增加可选 `sequence_bits` 参数（默认 4，校验 4~15），`timestamp_bits = 39 - 4 - sequence_bits` 总位数恒 39；`sharding.py` 三个生成器（uid/moment_id/topic_id）传入 `settings.*_sequence_bits` |
| P28-T3 | config.py 新增位宽配置 | 进行中 | 2026-08-22 | 新增 `uid_sequence_bits` / `moment_id_sequence_bits` / `topic_id_sequence_bits`（默认 4，环境变量 `UID_SEQUENCE_BITS` / `MOMENT_ID_SEQUENCE_BITS` / `TOPIC_ID_SEQUENCE_BITS`） |
| P28-T4 | seed_cli.py 超时环境变量化 | 完成 | 2026-08-22 | 新增 `SEED_HTTP_TIMEOUT`（默认 90s，httpx 客户端）与 `SEED_REQ_TIMEOUT`（默认 120s，`_req` 总超时），消除分钟边界 ReadTimeout 误报 |
| P28-T5 | 回归验证 | 完成 | 2026-08-22 | 内联脚本验证：默认 `sequence_bits=4` 生成 16 个 ID 互不相同/正数/位宽 ≤39（与改造前一致）；`sequence_bits=7` 同一分钟 128 个互不相同/正数/位宽 30；`sequence_bits=3/16` 被 ValueError 拒绝；ruff lint 全绿 |

