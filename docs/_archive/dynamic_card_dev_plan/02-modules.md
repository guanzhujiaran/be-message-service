[← 返回目录](./README.md)

## 二、功能模块划分

### 模块 1：Moment 发布服务 (`app/services/moment_publish.py`)

- [ ] 纯文字Moment创建（auditStatus 默认 `auditing`，支持正文富文本外链图片 URL）
- [ ] 转发Moment创建（含转发链溯源，校验源Moment为 `normal` 状态；创建时不 +repostCount，等审核通过再 +1）
- [ ] Moment编辑/删除（编辑非 `normal` 状态Moment → 自动回 `auditing` 重新审核；若被删/编辑的 FORWARD Moment before=normal，则对 srcDyn.repostCount -1）
- [ ] 空间置顶/取消置顶（仅 `normal` 状态Moment可置顶）
- [ ] 发布前置校验（权限、字数、@数量；MVP 限制 dynType ∈ {WORD, FORWARD}）

### 模块 2：Moment Feed 流服务 (`app/services/moment_feed.py`)

- [ ] 综合页 Feed（关注 + 推荐，支持分页游标；仅 `auditStatus='normal'`）
- [x] **关注流 Feed（`/feed/following`）**：仅展示当前登录用户**关注的人**发布的 normal 动态；关注 mid 集合经 `FollowService.list_following_mids` 全量取回过滤，复用综合页装配管线（2.2.0 新增）
- [ ] 个人空间 Feed（指定 UID 的Moment列表；**作者本人视角**包含 auditing/rejected，**访客视角**仅 normal）
- [ ] Moment 详情页（单条Moment完整渲染；非作者且非 normal → 返回 404 或审核中占位卡）
- [ ] 批量Moment 详情（批量 dyn_id 查询；非 normal 按权限过滤）
- [ ] 更新基线 & 历史偏移（支持下拉刷新 + 上拉加载）

### 模块 3：Moment 互动服务 (`app/services/moment_interaction.py`)

- [ ] 点赞 / 取消点赞（幂等，防重复计数；仅 normal 状态Moment可点赞）
- [ ] Moment举报
- [ ] 预约卡 / 投票卡等附加卡交互（后续迭代，MVP 预留接口占位）

### 模块 4：Moment 统计服务 (`app/services/moment_stat.py`)

- [ ] 统一「**明细表 + 计数原子增减**」工具函数封装（`incr_stat(dynId, field, delta)` / `decr_stat(...)`，供 like/repost/view/comment 共享；严禁 COUNT 聚合在请求热路径）
- [ ] **点赞 likeCount** 原子 ±1（由 dynamic_interaction 调用，配合 `TMomentLike` 明细唯一约束做幂等）
- [ ] **转发 repostCount** 原子 ±1（状态机 4 个关键点调用：approve/reject/编辑 normal 回审核/软删 normal 转发；修改对象是**源Moment**那一行）
- [ ] **浏览 viewCount** 原子 ±1（配合 `TMomentViewLog` 首次 upsert +1）
- [ ] **评论 commentCount** 原子 ±1（评论系统写明细后的回调入口）
- [ ] 统计数据批量读取（`SELECT * FROM "TMomentStat" WHERE "dynId" IN (...)`；直接读字段值，**不做 COUNT 聚合**）
- [ ] 夜间/运维对账脚本（明细 COUNT 与 Stat 不一致则修正；独立 CLI 或 APScheduler；非请求链路）
- [ ] 浏览记录去重（按 `mid + dynId + refDate`；ViewLog upsert 封装在此模块）

### 模块 5：Moment 话题 & 标签服务 (`app/services/moment_topic.py`)

- [ ] 话题广场列表
- [ ] 话题 Feed 流
- [ ] @用户推荐列表（最近联系/关注/粉丝）
- [ ] @用户搜索（按昵称模糊匹配）
- [ ] POI LBS 附近地点
- [ ] POI 关键词搜索

### 模块 6：Moment 数据库模型 (`app/models/dynamic_db.py`)

- [ ] 所有Moment 相关 ORM 模型（SQLModel）
- [ ] 枚举定义补充到 `app/models/enums.py`
- [ ] Alembic 迁移脚本（be-message MySQL 主库 `alembic/` 分支）

### 模块 7：Moment API 路由 (`app/api/moment.py`, `app/api/moment_feed.py`, `app/api/moment_audit.py`)

- [ ] Feed 流接口（综合页/空间页/话题页）
- [ ] Moment 发布 CRUD 接口
- [ ] 互动接口（点赞/举报）
- [ ] 审核管理接口（管理员权限守卫）
- [ ] 话题 & @ & POI 辅助接口

### 模块 8：Moment 事件联动（现有模块增强）

- [ ] 点赞Moment → 事件提醒（EventTypeEnum.LIKE，SourceTypeEnum.DYNAMIC）
- [ ] 评论Moment → 事件提醒（EventTypeEnum.REPLY，SourceTypeEnum.DYNAMIC）
- [ ] @用户在Moment中 → 事件提醒（EventTypeEnum.AT，SourceTypeEnum.DYNAMIC）
- [ ] **审核驳回** → 事件提醒（EventSubType=AUDIT_REJECT，含驳回原因给作者）
- [ ] Moment被转发 → 源Moment不发提醒；**repostCount 由状态机关键点（审核通过/驳回/编辑回审核/软删）原子 ±1 维护**

### 模块 9：Moment 审核服务 (`app/services/moment_audit.py`)

- [ ] 管理员待审核列表（按 auditing 时间倒序，分页）
- [ ] Moment 审核通过（auditStatus → normal + pubTime=now()；写 TMomentAuditLog；**不发通知**；若 dynType=FORWARD 则 srcDyn.repostCount 原子 +1）
- [ ] Moment 审核驳回（auditStatus → rejected；写 auditRejectReason + TMomentAuditLog + 发驳回事件通知；**若 dynType=FORWARD 且 before=normal 则 srcDyn.repostCount 原子 -1**）
- [ ] Moment重新审核（用户编辑 rejected Moment后 → auditing）
- [ ] 审核记录流水查询（管理员后台）
- [ ] 管理员权限校验（复用现有角色系统，仅 root/admin 可操作）

### 模块 10：Moment 空间统计服务（`app/services/moment_feed.py` 扩展）

- [x] 空间统计接口 `/upstat`：统计指定用户对外可见（`auditStatus='normal'` 且未软删）Moment 的**动态总数**与**获赞总数**（SUM(`TMomentStat.likeCount`)），对标 B 站 `/x/space/upstat`；低频统计接口，允许 COUNT/SUM 聚合。

### 模块 11：用户空间信息服务（`app/api/space.py` 新增，对标 B 站 `/x/space/wbi/acc/info`）

> 用户空间信息相关接口（`/space/info` 及 `feed/space`、`upstat`、`follow/stat`）统一归类到**空间信息**这一功能域下描述（2.12.0 新增）。

- [ ] 用户空间信息接口 `GET /api/v1/user/space/info?mid=`：返回单个用户完整空间资料（`mid`/`name`/`sex`/`face`/`sign`/`level`/`vip`/`birthday`/`official`/`pendant`/`nameplate`/`is_followed`/`top_photo` 等），数据源 `PptrUserService.get_user_profile`（pptr 四表联查）+ `FollowService.is_following`（关注态）；对标 B 站 `/x/space/wbi/acc/info?mid=`
- [ ] **不存在的用户专用错误响应**：`mid` 指向不存在的用户时返回 `code=USER_NOT_FOUND`（新增专用错误码）与明确 `msg`（如「用户不存在」），**不再**回退为空列表/`0`/`404` 兜底
- [ ] **黑名单空间访问拒绝（后端）**：空间读接口（`/space/info`、`feed/space/{mid}`、`upstat`、`follow/stat`）对与目标用户存在黑名单关系（`i_blocked` 或 `blocked_by`，经 `FollowService.get_relation` 判定）的请求返回 `403` + 明确 `msg`（如「对方已将你加入黑名单」），拒绝返回空间数据
- [ ] **黑名单空间访问拒绝（前端）**：空间页据 `/message/follow/relation` 返回的 `i_blocked`/`blocked_by` 做访问拦截——被拉黑/已拉黑对方时展示受限提示（对标 B 站黑名单空间互访限制）
