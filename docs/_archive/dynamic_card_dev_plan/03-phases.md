[← 返回目录](./README.md)

## 三、分阶段开发进度安排

### Phase 1：基础骨架 & 数据库

- [ ] **P1-T1**：在 `app/models/enums.py` 中新增Moment 相关枚举（MomentTypeEnum MVP 仅 WORD/FORWARD；AuditStatusEnum（auditing/normal/rejected/hidden）；VisibleScopeEnum；FoldTypeEnum；ReportReasonEnum 等）
- [ ] **P1-T2**：创建 `app/models/dynamic_db.py`，定义 ORM 模型：TMoment、TMomentStat、TMomentLike、TMomentTopic、TMomentViewLog、TMomentReport、TMomentAuditLog（MVP 不含 TMomentMedia，后续迭代补充）
- [ ] **P1-T3**：在 `app/models/__init__.py` 中导出新模型
- [ ] **P1-T4**：创建 Alembic 迁移脚本（`alembic/versions/`，be-message MySQL 主库分支），建表 + 索引 + FK
- [ ] **P1-T5**：验证迁移脚本可正常执行（`alembic -c alembic.ini upgrade head`，be-message MySQL 主库）

**阶段目标**：数据库评审通过，迁移脚本执行无报错

### Phase 2：Moment 发布 CRUD + 审核状态流转

- [ ] **P2-T1**：创建 `app/models/schemas/moment.py`（Request/Response SQLModel Schema）
- [ ] **P2-T2**：实现 `app/services/moment_publish.py` — 纯文字Moment创建（auditStatus 默认 `auditing`；校验 dynType=WORD；支持正文外链图片 URL 富文本节点）
- [ ] **P2-T3**：实现转发Moment创建（校验源Moment auditStatus=normal；repostSrcDynId 引用；转发深度计算；**创建时不 +repostCount，等审核通过再 +1**）
- [ ] **P2-T4**：实现Moment编辑/删除（编辑 rejected/auditing Moment → 重置为 auditing 重新审核；软删而非硬删；**若被删/编辑的 FORWARD Moment before=normal，则对 srcDyn.repostCount -1**）
- [ ] **P2-T5**：实现空间置顶/取消置顶（仅 `normal` 且本人可操作）
- [ ] **P2-T6**：实现发布前置校验（权限、字数上限、@数量上限；MVP 拒绝 WORD/FORWARD 以外的 dynType）
- [ ] **P2-T7**：创建 `app/api/moment.py` 发布类路由并注册到 main.py
- [ ] **P2-T8**：编写单元测试

**阶段目标**：可发布文字/转发Moment，流转 auditing → 等待审核；支持编辑/删除/置顶

### Phase 3：Feed 流 & 详情

- [ ] **P3-T1**：实现 `app/services/moment_feed.py` — 综合页 Feed（仅 `auditStatus='normal'`；关注列表 + 时间倒序）
- [ ] **P3-T2**：实现个人空间 Feed（本人：全部状态，访客：仅 normal；置顶Moment优先排序）
- [ ] **P3-T3**：实现Moment 详情页（状态过滤 + 权限判断；repostCount 直接读 TMomentStat 字段）
- [ ] **P3-T4**：实现批量Moment 详情（批量 dyn_id 查询，限 20 条；状态过滤）
- [ ] **P3-T5**：实现分页游标方案（updateBaseline + historyOffset + hasMore + updateNum）
- [ ] **P3-T6**：创建 `app/api/moment_feed.py` Feed 路由并注册
- [ ] **P3-T7**：编写单元测试

**阶段目标**：MVP 闭环（发布 → auditing → 手动改 normal → Feed 可见 → 详情可看）

### Phase 4：互动 & 统计

- [ ] **P4-T1**：实现 `app/services/moment_stat.py` — 统一「明细表 + 计数原子增减」工具封装（incr_stat/decr_stat）；likeCount / repostCount / viewCount / commentCount **全部走原子 UPDATE**；批量读取直接读 TMomentStat 字段；严禁请求热路径做 COUNT 聚合
- [ ] **P4-T2**：实现 repostCount 状态机 4 个触发点的原子 ±1（由 P2/P6 调用本模块函数；含 forward Moment before/after 状态判断）
- [ ] **P4-T3**：实现 `app/services/moment_interaction.py` — 点赞/取消点赞（幂等；仅 normal；事务「INSERT/DELETE TMomentLike」 + 「UPDATE likeCount ±1」同事务；唯一冲突跳过计数）
- [ ] **P4-T4**：实现浏览去重（TMomentViewLog upsert；首次新行时才 +viewCount）。**触发方式：由后端在动态详情接口 `GET /api/v1/moment/detail/{moment_id}` 被真实访问时自动累计**，仅登录用户（`x-bili-mid` 解析出的 viewer）计数，按 `mid+dynId+refDate` 去重；**不提供前端主动上报接口**
- [ ] **P4-T5**：实现Moment举报（写 TMomentReport；不改变 auditStatus）
- [ ] **P4-T6**：实现夜间/运维对账脚本（可选 CLI 或 APScheduler job；明细 COUNT 与 Stat 不一致则修正）
- [ ] **P4-T7**：在 `app/api/moment.py` 中新增互动类路由（**浏览不计入互动上报路由，浏览在 feed 详情路由内部自动累计**）
- [ ] **P4-T8**：编写单元测试（点赞幂等、浏览去重、repostCount 4 触发点 ±1、举报写入、计数器负数兜底）
- [ ] **P4-T9**：评论举报（对接真实接口）：新增 `TCommentReport` 表（唯一约束 `rpid+reportMid`，一人一条去重）；`CommentService.report` 幂等写入 + 统计有效举报数，达 `settings.comment_report_threshold`（默认 3）时把 `CommentIndex.state` 由 `normal` → `auditing`；`app/api/comment.py` 注册 `POST /report`（举报原因复用 `MomentReportReasonEnum`）；前端评论项「举报」接该接口、「加入黑名单」接 `POST /message/follow/block`（`target_mid=评论作者mid`）
- [ ] **P4-T10**：动态收藏夹系统（后端）：新增 `TFavoriteFolder`（多夹+默认夹）/`TMomentFavorite`（唯一约束 `dynId+folderId`）/`TUserFavoriteSetting`（主页收藏可见性）；`FavoriteService` 收藏夹 CRUD、默认夹 get_or_create、收藏/取消（favoriteCount 按用户去重）、夹内动态分页、可见性 get/set；`app/api/favorite.py` 注册 `/api/v1/favorite/*` 写入与本人读接口
- [ ] **P4-T11**：他人主页收藏公开读（后端）：`FavoriteService.list_public_folders` / `list_public_dyn_ids`（受主人 `showFavorites` 控制，不公开返回 None）；`app/api/favorite.py` 注册 `GET /user/folders` / `GET /user/dynamics`（无需登录）；网关 `ProxyEndPort.js` 增加 `/api/v1/favorite` 代理、`JwtModule.js` 白名单放行 `user/folders` / `user/dynamics`

**阶段目标**：点赞/浏览/举报功能可用，统计数据准确；repostCount 通过状态机原子 ±1 正确显示

### Phase 5：话题 & @ & LBS

- [ ] **P5-T1**：实现 `app/services/moment_topic.py` — 话题广场列表
- [ ] **P5-T2**：实现话题 Feed 流
- [ ] **P5-T3**：实现 @用户推荐列表（最近联系/关注/粉丝分组）
- [ ] **P5-T4**：实现 @用户搜索（昵称模糊匹配）
- [ ] **P5-T5**：实现 POI LBS 附近地点搜索
- [ ] **P5-T6**：实现 POI 关键词搜索
- [ ] **P5-T7**：在 `app/api/moment.py` 中新增辅助类路由
- [ ] **P5-T8**：编写单元测试

**阶段目标**：辅助功能开发完成

### Phase 6：审核服务 & 事件联动

- [x] **P6-T1**：实现 `app/services/moment_audit.py` — 管理员待审核列表（分页 + 按 auditing→时间倒序）
- [x] **P6-T2**：实现审核通过（auditStatus→normal + pubTime=now()；写 TMomentAuditLog；**不发通知**；**dynType=FORWARD 时 srcDyn.repostCount +1**）
- [x] **P6-T3**：实现审核驳回（auditStatus→rejected；写 auditRejectReason + TMomentAuditLog + **发驳回事件通知**；**dynType=FORWARD 且 before=normal 时 srcDyn.repostCount -1**）
- [x] **P6-T4**：实现审核记录流水查询（管理员后台，按 dynId / 管理员 / 时间段过滤）
- [x] **P6-T5**：管理员权限校验（复用 `get_current_admin_user` 或 role=root 守卫装饰器）
- [x] **P6-T6**：点赞Moment → 事件提醒（EventTypeEnum.LIKE + SourceTypeEnum.DYNAMIC）
- [x] **P6-T7**：评论Moment → 事件提醒（EventTypeEnum.REPLY + SourceTypeEnum.DYNAMIC；复用评论系统已有事件链路，新增 DYNAMIC SourceType 分支）
- [x] **P6-T8**：Moment正文 @用户 → 事件提醒（EventTypeEnum.AT + SourceTypeEnum.DYNAMIC；发布时解析 AT 节点批量生产）
- [x] **P6-T9**：在 `app/api/moment_audit.py` 中新增管理员审核路由并注册（role 守卫）
- [x] **P6-T10**：编写单元测试

**阶段目标**：审核流完整（auditing → normal/rejected + 驳回通知）；事件中心联动完整

### Phase 7：接口联调 & 测试

- [x] **P7-T1**：全链路冒烟测试（发布→auditing→管理员通过→Feed 可见→点赞→评论→转发→再次通过转发→repostCount +1 校验→删除转发→-1 校验）——2026-08-17 由后续单测/回归覆盖（见 08-task-records Phase 7 注记）
- [x] **P7-T2**：审核流专项测试（驳回通知到达、编辑 rejected 后回 auditing、通过不发通知）——P6-T10 单测 + 2.23.0 回归
- [x] **P7-T3**：边界用例测试（空内容、超长文本、@数量上限、转发源为 rejected/已删 → 拒绝；非本人操作）——P2-T8 / P5-T8 单测
- [x] **P7-T4**：权限测试（非作者无法编辑/删除/置顶；非管理员无法调用审核接口）——P2/P6 单测 + 2.22.1 403 回归
- [x] **P7-T5**：并发测试（点赞幂等原子性、浏览去重幂等性、repostCount 审核/删除并发 ±1 一致性、计数器负数兜底保护）——P4-T8 单测 + 2.15.2 对账
- [x] **P7-T6**：性能测试（Feed 流分页、批量详情、Stat 批量 IN 查询 SQL EXPLAIN；对账脚本对大数据量 COUNT 表现验证）——P8-T10 灌数 + 2.11.1/2.15.2 对账
- [x] **P7-T7**：Bug 修复 & 代码 review——各版本 bugfix 持续执行
- [x] **P7-T8**：接口文档更新——05-api.md + README changelog 随版本同步；前端 SDK 重新生成

**阶段目标**：功能验收通过，可部署上线

### Phase 8：空间统计接口（对标 B 站 upstat）

- [x] **P8-T1**：在 `app/services/moment_feed.py` 新增 `get_upstat(mid)`：`COUNT(TMoment.dynId)` 得 `dynamic_count`，JOIN `TMomentStat` 后 `SUM(likeCount)` 得 `like_count`；仅统计 `normal` + 未软删。在 `app/models/schemas/moment.py` 新增 `MomentUpStatResp(mid, dynamic_count, like_count)`；在 `app/api/moment.py` 注册 `GET /upstat`（公开，无需登录，参数 `vmid`）。前端 `moment-api.ts` 新增 `fetchUpStat` 封装、`MomentSpaceView` 空间页统计栏接入（动态/获赞改用本接口，移除本地聚合）。
- [x] **P8-T2**：测试数据种子脚本 **`scripts/seed_moment_via_api.py`（纯 HTTP 接口调用版，不直写数据库）**：从 pptr Postgres 随机取真实用户（只读回查），通过 HTTP 走完整业务链路生成数据 —— ① 作者 `POST /moment/create` 发布（auditing）② 管理员(role=root) `POST /moment/audit/approve` 审核通过（normal）③ 其他用户 `POST /moment/thumb` 点赞（TMomentLike 明细 + likeCount 原子 +1）④ 其他用户 `POST /comment/add` 评论（type=DYNAMIC，oid=dynId）⑤ 作者 `POST /moment/repost` 转发 + 再次审核通过 ⑥ `POST /message/follow/do` 建立关注关系。支持 `--count / --users / --base-url / --admin-mid / --skip-follow`；用于模拟生产环境、验证 Feed / upstat / 评论 / 点赞 / 转发 / 关注流全链路接口功能。
- [x] **P8-T3**：关注流 Feed 接口：在 `FollowService` 新增 `list_following_mids(mid)`（全量取关注 mid 集合，按关注时间倒序）；在 `app/services/moment_feed.py` 新增 `following_feed(viewer_mid)`：`TMoment.mid IN (关注集合)` + normal + 未软删 + pubTime 非空，按 pubTime 倒序，复用综合页装配管线；在 `app/api/moment_feed.py` 注册 `GET /feed/following`（**必须登录**，RequiredUser，未登录 401）。对标 B 站「关注动态」首页 Tab。
- [x] **P8-T6**：前端评论 + 转发模块（纯前端接入，无新增后端接口）：
  - **评论**：复用通用评论区 `LotteryCommentSection`（`type=COMMENT_TYPE.DYNAMIC`、`oid=dynIdStr`、`up-mid=作者mid`）。**信息流（AllFeed / TopicFeed / Space）卡片点评论按钮 → 卡片内下拉展开评论区**（对标 B 站动态，不跳转，`inlineComment` 模式，首次点击才懒加载渲染）；**详情页（MomentDetailView）改为 el-tabs 结构（动态 / 评论 两个 tab），点卡片评论按钮 → 切换到评论 tab**（`inlineComment=false` 时 emit `comment`）。`LotteryCommentSection` 增加 `count-change` 事件，联动更新 `stat.commentCount`
  - **转发**：新增 `MomentRepostDialog` 转发弹窗组件（输入转发语 → 构造 WORDS 节点 → `repostMoment({ srcDynId, content })`）；`MomentStatBar` 转发按钮可点击 → 打开弹窗；转发成功后乐观更新 `stat.repostCount +1`
  - 覆盖入口：信息流卡片（AllFeed / TopicFeed / Space）+ 详情页（MomentDetailView）
- [x] **P8-T7**：评论匿名受限（对标 B 站未登录仅展示前 10 条）：
  - **网关**（be-gateway）：全局 `jwtAuth` 白名单补充评论**读接口**（`/api/v1/comment/main`、`/api/v1/comment/detail/*`、`/api/v1/comment/sub`），未登录可读（此前被全局 jwtAuth 拦截返回 -101）；评论**写接口**（add/delete/reply 等）仍走 jwtAuth，由上游 RequiredUser 兜底登录校验
  - 后端 `GET /api/v1/comment/main`：`viewer_mid=None` 时强制将 `page_size` 收拢到 `10`（不信任前端传入值，最大 10 条）；`CommentListResp` 新增 `viewer_is_anonymous: bool` 字段（默认 false；匿名访问 = true）
  - 前端 `LotteryCommentSection`：在 `viewerIsAnonymous=true` 时，评论列表底部渲染半透明蒙层（`bg-black/40` + `backdrop-blur`）+ 「登录」按钮（点击调起登录）与引导文案「登录后查看全部评论」；已有评论正常展示，蒙层覆盖第 11 条及以后的位置
  - 覆盖入口：所有使用 `LotteryCommentSection` 的页面（动态卡片内嵌 / 详情页评论 tab）
- [x] **P8-T8**：详情页布局改造（对标 B 站）：
  - 去除顶部「返回」header（路由切换与浏览器后退保持原行为）
  - 右侧新增悬浮工具栏（`position: fixed`，随窗口滚动）：点赞 / 收藏 / 转发 / 评论 / **浏览** 五个图标 + 各自计数（点击评论图标 → 切到「评论」tab；点击转发 → 打开转发弹窗）；「浏览」图标展示 `stat.viewCount`（后端 `_build_stat_modules` 已随详情返回该字段），仅作展示不可点击
  - **图标统一使用 `src/assets/svgs/dynamic/detail/side_toolbar/` 下的 SVG**（`?component` 导入）：`like.svg`（点赞）/ `favorite.svg`（收藏）/ `forward.svg`（转发）/ `comment.svg`（评论），浏览仍用 element-plus `View` 图标（该目录暂无 view.svg）；「赞与转发」tab 列表内的类型图标同样复用 `like.svg` / `forward.svg`
  - 主区域顶部 `el-tabs`：「评论」「赞与转发」两个 tab（默认「评论」）；「赞与转发」内部再分「赞」「转发」两个子 tab（`el-tabs` 嵌套，对标 B 站）
- [x] **P8-T9**：新增点赞明细 / 转发列表接口（公开可读）：
  - `GET /api/v1/moment/{dynId}/likers`：查询 `TMomentLike` 按 `created_at` 倒序分页（`page_num` / `page_size`，默认 20/50），关联 `PptrUserService.get_many` 取作者简要（uname/face），返回 `{mid, uname, face, like_time}`
  - `GET /api/v1/moment/{dynId}/forwards`：查询 `TMoment WHERE repostSrcDynId=dynId AND auditStatus=normal AND deletedAt IS NULL` 按 `pubTime` 倒序分页，返回 `{dynId, mid, uname, face, pubTime, text}`（text 为转发时的 desc 模块文本）
  - `models/schemas/moment.py` 新增 `MomentLikerItem` / `MomentForwardItem` / `MomentLikerListResp` / `MomentForwardListResp`
  - 覆盖入口：详情页「赞与转发」tab 子列表
- [ ] **P8-T10**：性能测试 / 联调大数据灌数脚本 **`scripts/seed_moment_bulk.py`（直写 MySQL 批量灌数版，区别于 P8-T2 的纯 HTTP 版）**：
  - 数据源：直连 `biliopusdb`（普通抽奖动态库，be-bilibili-crawler 维护）流式拉取真实动态正文（`t_lotdyninfo.dynContent`，pubTime>2000 且正文非空共约 52 万条）、真实话题（`t_lot_extra_info.required_topic_text` 去重）、真实作者（`up_uid` 去重）、真实计数（commentCount / repostCount）
  - 灌入 BiliMessageDB 全 Moment 相关表：TMoment 主表（默认 10 万条，`--count` 可调）+ TMomentStat（1:1 行数对齐）+ TMomentTopic（真实话题全量）+ TMomentLike / TMomentViewLog / TMomentAuditLog 关联明细
  - 数据分布：auditStatus 按正常 90% / 审核中 5% / 驳回 5% 混合；pubTime 沿用真实值；likeCount 按真实点赞分布抽样，TMomentLike 明细与 likeCount 对齐
  - 参数：`--count`（默认 100000）、`--clean`（先清空相关表再灌）、`--batch-size`（默认 2000）、`--dry-run`
  - 幂等：dynId 沿用真实 bilibili dynId（天然唯一），`--clean` 提供一键重建；重复执行跳过已存在 dynId
  - 用途：P7-T6 性能测试（Feed 分页 / 批量详情 / Stat 批量 IN 查询 EXPLAIN / 对账脚本大数据量 COUNT 验证）+ 开发环境联调

**阶段目标**：空间页「动态数 / 获赞数」由后端专用接口返回，替代前端本地聚合；提供可复现的测试数据注入手段（纯接口链路 + 直写库大数据灌数）；支持「关注的人动态」独立 Feed 流；动态卡片与详情页评论、转发交互完整可用；未登录用户仅看前 10 条评论并引导登录；详情页布局对齐 B 站（悬浮工具栏 + 评论/赞与转发 双 tab）

### Phase 9：用户空间信息（对标 B 站 acc/info + 黑名单访问控制）

- [ ] **P9-T1**：`bili_common.models.ResponseCode` 新增专用错误码 `USER_NOT_FOUND`（如 `1008`）
- [ ] **P9-T2**：新增 `app/api/space.py`（或扩展现有用户路由），实现 `GET /api/v1/user/space/info?mid=`：`PptrUserService.get_user_profile` 四表联查拼装空间资料 DTO + `FollowService.is_following` 填 `is_followed`；`mid≤0` 返回 400，用户不存在返回 `USER_NOT_FOUND`
- [ ] **P9-T3**：黑名单空间访问拒绝（后端）：空间读接口（`/space/info`、`feed/space/{mid}`、`upstat`、`follow/stat`）调用 `FollowService.get_relation` 判定 `i_blocked`/`blocked_by`，命中即返回 403 + 明确 msg
- [ ] **P9-T4**：前端空间页（`MomentSpaceView`）改调 `/user/space/info` 获取用户资料（替代从动态 author 模块间接取 name/face）；据 `/message/follow/relation` 的 `i_blocked`/`blocked_by` 做黑名单访问拦截与受限提示
- [ ] **P9-T5**：编写单元测试（空间资料字段映射、用户不存在错误码、黑名单互访拒绝）

**阶段目标**：单用户空间资料由专用接口完整返回（对标 B 站 acc/info）；不存在的用户返回明确错误码而非空数据；黑名单用户在前后端均无法互访空间。

### Phase 10：评论计数口径修复 + 卡片举报按钮统一

- [x] **P10-T1**：评论计数同步 `root_count`/`all_count`（bugfix）：`CommentAdminService.set_state` 状态变化时，除回写 `TMomentStat.commentCount` 外，同步评论系统 `msg_comment_subject.root_count`（一级）/`all_count`（全部）——进入 `NORMAL` → +1、离开 `NORMAL`（→auditing/rejected/hidden）→ -1（floor 兜底）；使 Feed/详情展示的 `stat.commentCount`（读 `root_count`）严格等于当前 `NORMAL` 评论数
- [x] **P10-T2**：卡片右上角举报按钮统一（前端）：动态广场 `AllFeedView` / 话题广场 `TopicFeedView` 卡片传 `:show-more-actions="true"`（开启右上角 More 菜单「举报」项，复用 `MomentCard.handleReport` + `reportMoment`）；TopicFeed 补绑 `@report`；使广场卡片与动态详情页卡片右上角交互一致
- [x] **P10-T3**：单元测试（评论审核状态翻转后 `root_count`/`commentCount` 同步正确；被驳回评论不再计入评论数量）

**阶段目标**：评论计数严格只统计审核通过的评论；动态广场 / 话题广场 / 详情页卡片右上角举报按钮交互统一。

### Phase 11：统一举报系统（评论 / 动态 / 用户空间三类，RPA 独立）

- [ ] **P11-T1**：新增统一举报表 `TReportRecord`（`biz_type`=dynamic/comment/user、`biz_id`=dynId/rpid/mid、`accused_mid`、`report_mid`、`reason_type`、`reason_desc`、`pics`、`audit_status`、`audit_remark`、`audit_admin_mid`、唯一键 `(report_mid, biz_type, biz_id)`）+ alembic 迁移；新增统一来源枚举 `ReportBizTypeEnum`(dynamic/comment/user) + 统一原因枚举 `ReportReasonEnum`（对齐 B 站：垃圾广告/引战/辱骂/人身攻击/色情/违法违规/涉政谣言/虚假不实信息/违法信息外链/其他）
- [ ] **P11-T2**：统一举报接口 `POST /api/v1/report`（biz_type+biz_id+reason_type+reason_desc+pics，校验 biz 对象存在、pics 为 http(s) URL 列表 1-3 张）；统一管理端 `GET /api/v1/report/admin/list`（biz_type 过滤+分页）、`POST /api/v1/report/admin/review`（decision）；服务 `ReportService`
- [ ] **P11-T3**：业务**分开计算**联动——评论举报达阈值(3)转 `CommentIndex` auditing（复用 `CommentAdminService`）；动态举报达阈值转 `TMoment` auditing；用户空间举报达阈值可选处理；三者计数互不合并；统一幂等（一人一对象一次）
- [ ] **P11-T4**：存量数据迁移——`TMomentReport`→(biz_type=dynamic, biz_id=dynId)、`CommentReport`→(biz_type=comment, biz_id=rpid) 迁入 `TReportRecord`（迁移脚本 + 校验对账）；RPA `ResourceReport` **不并入**（保持独立归 RPA 管理）
- [ ] **P11-T5**：旧接口兼容/切换——`POST /moment/report`、`POST /comment/report` 内部改调统一 `ReportService`（RPA 的 `/api/admin/rpa/reports/*` 保持不动）
- [ ] **P11-T6**：前端统一举报弹窗组件 `ReportDialog`（B 站风格：预设原因 radio + 其他输入 0/60 + 图片附件区 pics）+ 接入动态（MomentCard/Detail/Feed/Space）、评论（LotteryCommentItem）、**用户空间**（MomentSpaceView）；新增 `reportByBiz` 封装
- [ ] **P11-T7**：单元测试（举报幂等、biz 对象校验、pics 校验、评论阈值转审、动态阈值转审、用户空间举报、迁移对账）

**阶段目标**：评论 / 动态 / 用户空间三类举报统一到一张表，通过 `biz_type`+`biz_id` 区分来源；动态与评论**分开计算**不合并；RPA 保持独立；统一举报接口、原因枚举、管理端审核闭环与前端弹窗组件。

### Phase 12：用户注销流程（彻底删除账号及其业务数据）

> **方案（2.15.1 调整）**：注销改为**分领域删除服务 + 消息队列编排**——先把各业务「按 uid 彻底清除」的删除逻辑拆成**独立、可复用的领域服务**（动态 / 评论 / 关注 / 举报 / 收藏 / 私信 / 通知 / 事件 / 设置活跃封禁管理 / pptr 用户），每个领域服务提供 `delete_all_by_uid(session, uid)` 类方法；`UserDeactivateService` 只负责**组合编排**这些领域服务；注销接口**投递 MQ 消息**（`message.user.deactivate` 队列），由消费者异步执行完整注销流程（先 pptr 后 be-message），避免接口同步阻塞删除大用户数据的耗时。

- [ ] **P12-T1**：注销消息载体 + 队列：`models/schemas/mq.py` 新增 `UserDeactivatePayload(uid)`；`app/core/broker.py` 新增 `RK_USER_DEACTIVATE` + `user_deactivate_queue`；`app/services/publisher.py` 新增 `publish_user_deactivate(uid)`（异常吞掉返回 bool）
- [ ] **P12-T2**：**领域删除服务拆分**（`app/services/cleanup/`，每个服务 `delete_all_by_uid(session, uid)`，逻辑从现有 `user_deactivate.py` 的 SQL 列表迁移）：
  - `cleanup_moment.py`：`TMoment`(mid) 级联子表 + 我点赞/浏览他人动态痕迹（`TMomentLike`/`TMomentViewLog` 的 mid）
  - `cleanup_comment.py`：`msg_comment_content`(rpid 子查询) / `msg_comment_index`(mid) / `msg_comment_action`(mid) / `msg_comment_at`(from_mid/at_mid) / `msg_comment_subject`(up_mid)
  - `cleanup_follow.py`：`msg_user_follow`(mid/target_mid，双向关注与拉黑)
  - `cleanup_report.py`：`TMomentReport` / `CommentReport` / `TUserReport`(reportMid/accusedMid)
  - `cleanup_favorite.py`：`TFavoriteFolder` / `TMomentFavorite` / `TUserFavoriteSetting`(mid)
  - `cleanup_dm.py`：`msg_dm_session` / `msg_dm_index`(owner/sender) / `msg_dm_content_dlq`(sender/receiver)
  - `cleanup_notify.py`：`msg_notify_cursor` / `msg_notify_state` / `msg_notify`(creator_mid)
  - `cleanup_event.py`：`msg_event`(mid/actor_mid) / `msg_event_cursor`(mid)
  - `cleanup_misc.py`：`msg_user_setting` / `msg_user_activity` / `msg_user_ban`(mid/operator_mid) / `msg_admin`(mid/granted_by)
  - `cleanup_pptr.py`：pptr Postgres 四表 `TUserInfo`/`TUserDetail`/`TUserLevel`/`TUserVip` + 日志表 `TUserActInfoLog`/`TUserExpRecord`/`TUserNameRecord`/`TUserPwdRecord`（pptr 单独 engine）
- [ ] **P12-T3**：`UserDeactivateService` 重构为**编排器**：`deactivate(uid)` 校验 uid 后按依赖顺序依次调用各 `cleanup_*` 领域服务（pptr 先、be-message 后，各自单事务，整体串行）；幂等（已注销/无数据仍正常返回）；删除顺序/级联说明迁移到各 cleanup 服务 docstring
- [ ] **P12-T4**：MQ 消费者：`app/mq/consumers/deactivate.py` 注册 `@router.subscriber(user_deactivate_queue, ack_policy=MANUAL)` → handler 调 `UserDeactivateService.deactivate(uid)`，成功 ack，失败写日志/转死信不 requeue；`app/consumers/deactivate.py` 实现 `handle_user_deactivate(payload, msg)`；`app/mq/consumers/__init__.py` import 注册
- [ ] **P12-T5**：注销接口改投递 MQ：`POST /api/v1/user/deactivate`（本人，`CurrentUser` 校验 uid）与 `POST /api/v1/user/admin/deactivate`（`AdminUser`/root 注销他人）——接口**只校验 + 投递** `publish_user_deactivate(uid)`，返回「已提交注销」；真实删除由消费者异步完成（避免同步阻塞）
- [ ] **P12-T6**：JWT 失效：注销后撤销当前/目标 token（复用 be-gateway Redis 签名黑名单），并联动 `msg_user_ban`/黑名单清理
- [ ] **P12-T7**：前端用户设置页新增「注销账号」入口（二次确认 + 输入校验，对标 B 站），调 `deactivateUser`；注销成功跳登录/清除本地态
- [ ] **P12-T8**：单元测试（各 cleanup 领域服务按 uid 删除正确、级联清理、编排顺序、重复注销幂等、接口校验 + MQ 投递、权限拦截）

**阶段目标**：各业务删除逻辑拆分到独立可复用领域服务；注销由接口投递 MQ、消费者异步执行；注销后 pptr 四表物理删除、be-message 全部业务数据彻底清除、JWT 失效，账号不可恢复（Casdoor 不管）。

### Phase 13：用户头像修改（仅支持图片 URL + 后端下载校验）（2.16.0）

> **方案**：修改头像**只接受图片 URL 链接**（不接收文件上传），后端主动下载该 URL 做两道校验后入库：
> ① **1s 内下载完成**（`httpx.AsyncClient` 超时 1s，超时/网络错误即拒）；
> ② **文件大小 ≤ 1MB**（流式读取累计字节，超限即断连拒绝，防止大文件/慢速源拖垮接口）。
> 校验通过后复用现有 `PptrUserService.set_user_detail(face=...)` 写入 pptr `TUserDetail.avatar`。

- [ ] **P13-T1**：`bili-common` `PptrUserInfoUpdateParams` 新增 `avatar` 字段（`str = Field(default="", max_length=1024)`，图片 URL）
- [ ] **P13-T2**：新增头像 URL 下载校验服务（`app/services/avatar_check.py`）：`verify_avatar_url(url) -> bool`——httpx 流式下载，超时 1s、累计 ≤1MB、仅允许 http/https、仅响应 Content-Type 为图片（image/*）
- [ ] **P13-T3**：`POST /api/v1/user/user_info/update` 接入 `avatar`：非空时先 `verify_avatar_url` 校验（失败返回 422 具体原因），通过后 `set_user_detail(face=avatar)` 落库
- [ ] **P13-T4**：前端用户中心首页 `UserCenterDefaultPanel.vue` 头像卡片新增「修改头像」入口：弹窗输入图片链接 → 调 `UpdateUserInfo`（`user_base_info_config_form` 已含 `avatar` 字段）→ 成功后刷新头像显示
- [ ] **P13-T5**：单元测试：URL 校验（大小超限 / 超时 / 非图片 / 非法协议拒绝，正常小图通过）、接口透传落库、前端弹窗交互

**阶段目标**：用户可在用户中心首页自助修改头像（仅图片 URL）；后端保证图片在 1s 内可下载且 ≤1MB，避免拖慢接口与存入失效/超大外链。

### Phase 14：话题创建与审核（2.19.0，对齐动态发布审核流程）

> **方案**：话题从「seed 灌入只读」扩展为「用户可创建 + 先审后发」。`TMomentTopic` 直接新增审核字段（`auditStatus`/`auditRejectReason`/`pubTime`/`creatorMid`），对齐 `TMoment` 审核范式（P6）：用户创建 → auditing（不公开）→ 管理员通过（normal+pubTime=now()，不发通知）/ 驳回（rejected+原因，发驳回通知给创建者）。话题广场/Feed/热搜统一只展示 normal 话题；发布动态关联话题时校验话题 normal（否则 422）。

- [x] **P14-T1**：数据模型——`TMomentTopic` 新增 `auditStatus`(auditing/normal/rejected, DEFAULT 'auditing')、`auditRejectReason`(VARCHAR 500, NULL)、`pubTime`(datetime, NULL)、`creatorMid`(BIGINT, DEFAULT 0)；新增索引 `idx_topic_audit_created`(auditStatus, createdAt DESC) + `idx_topic_creator_created`(creatorMid, createdAt DESC)；`models/enums.py` 新增 `MomentTopicAuditStatusEnum`（auditing/normal/rejected）；alembic 迁移（加列 + 索引），存量 seed 话题置 `auditStatus='normal'`、`pubTime=now()`、`creatorMid=0`；**`topicId` 由自增改为应用层雪花 ID**（`topicId` 非自增，`generate_topic_id()` 生成；雪花生成器下沉 `bili-common/core/snowflake.py`，新增规则 `snowflake-id.mdc`）——2026-08-16 完成
- [x] **P14-T2**：创建话题服务 + 接口——`MomentTopicService.create_topic(session, mid, topic_name, topic_cover, topic_desc)`：名称唯一校验（重复 422）、长度/封面 URL(http/https)/描述校验；创建即 `auditStatus='auditing'`、`pubTime=NULL`、`creatorMid=mid`；`POST /api/v1/moment/topic/create`（`CurrentUser`）返回新话题（含 auditStatus='auditing'）——2026-08-16 完成
- [x] **P14-T3**：管理端话题审核服务——`MomentTopicAuditService`：`pending_list`（auditing 队列分页，回查创建者 uname/face）、`approve`（→normal+pubTime=now()，**不发通知**）、`reject`（→rejected+原因，**发驳回通知给创建者**，复用 NotifyService/事件）；`app/api/moment_topic_audit.py`（root 守卫）注册 `GET /list`、`POST /approve`、`POST /reject`；`main.py` 注册——2026-08-16 完成
- [x] **P14-T4**：对外可见性过滤——`/topic/square`、`/topic/hot-search`、话题 Feed `/feed/topic/{topicId}` 的 SQL 统一加 `auditStatus='normal'` 过滤；话题 Feed 目标话题非 normal 返回空；`/topic/detail` 同理（游客与作者均不可见非 normal 话题）——2026-08-16 完成
- [x] **P14-T5**：发布动态关联话题校验——`moment_publish` 关联话题时校验 `TMomentTopic.auditStatus='normal'`，否则 422 拒绝（auditing/rejected 话题不可被动态引用）——2026-08-16 完成（2.22.0 起改 400，见 P18-T3）
- [x] **P14-T6**：`GET /api/v1/moment/topic/mine`（`CurrentUser`）——我创建的话题（含全部状态），每话题返回 auditStatus/auditRejectReason/pubTime，按 createdAt 倒序——2026-08-16 完成
- [x] **P14-T7**：前端话题广场创建入口——`TopicSquareView` 新增「创建话题」按钮（弹窗：名称/封面/描述 → `createTopic` → 成功提示「已提交审核」）；新增 `GET /topic/mine` 展示「我创建的话题」及审核状态徽标（auditing=待审核 / rejected=驳回原因）——2026-08-16 完成
- [x] **P14-T8**：前端管理后台「话题审核」页——`AdminLayout` 新增「话题审核」入口（root）+ `TopicAuditListView.vue`（列表：创建者/名称/封面/描述/提交时间 + 通过/驳回弹窗），路由 `ADMIN_MOMENT_TOPIC_AUDIT`——2026-08-16 完成
- [x] **P14-T9**：SDK 更新后接入（`POST /topic/create`、`GET /topic/mine`、管理端 `/topic/audit/*`），moment-api.ts 薄封装 `createTopic`/`fetchMyTopics`/`fetchTopicAuditList`/`topicAuditApprove`/`topicAuditReject`——2026-08-16 完成
- [ ] **P14-T10**：单元测试——创建（名称唯一/长度/封面 URL/auditing 状态）、审核（approve→normal+pubTime / reject→rejected+原因+驳回通知）、广场/Feed/热搜过滤（非 normal 不展示）、动态关联非 normal 话题 422、`mine` 仅本人、管理端权限（非 root 拒绝）——独立单测待补（链路已由 2.19.0 API 验证覆盖）

**阶段目标**：用户可自助创建话题并进入审核；管理员在管理后台审核通过/驳回（对齐动态审核流程）；话题广场/Feed/热搜只展示审核通过的话题；发布动态只能引用已审核通过的话题；前端完成创建入口、我的话题、管理端审核页。

### Phase 15：抽奖卡片互动补全（2.20.0，点赞/收藏/转发到动态）

> **方案**：动态 RESOURCE 节点与通用互动（`InteractionBizTypeEnum.LOTTERY`）已支持 lottery，本阶段补齐四块：① lottery 资源存在性**跨服务 RPC 校验**（此前默认放行）——be-message 侧新增 `LotteryRpcClient`（经 RabbitMQ RPC 弱依赖调 be-bilibili-crawler），注册进 `InteractionResourceValidator`（bizType=lottery），点赞/收藏/转发 lottery 时校验资源存在，不存在 422；② 抽奖卡片**转发到动态**——复用 `POST /moment/create` 的 RESOURCE 节点（bizType=lottery/bizId/name，**不存卡片快照**）发布新动态，发布时经 `LotteryRpcClient` 校验存在；③ **读取动态时实时获取 attach 卡片详情**——Feed/详情装配管线（`_build_feed_item`）对 RESOURCE=lottery 节点经 RPC 从 be-bilibili-crawler 实时拉取 lottery 详情（title/cover/jumpUrl）填充后返回前端，**不依赖数据库快照**；④ 前端抽奖卡片 `BiliLotteryCard` 新增**点赞/收藏/转发**按钮（列表卡片 + 详情页）。

- [x] **P15-T1**：be-bilibili-crawler 提供 lottery 校验 RPC 方法——在 lottery RPC server 新增 `check_lottery_exist`（入参 `lotteryId`，返回是否存在/详情），契约并入 `bili_common/rpc/lottery.py`
- [x] **P15-T2**：be-message `LotteryRpcClient`——新增 `app/services/lottery_rpc.py`（参照 `rpa_rpc.py`：`RpcClient` + 弱依赖降级，超时/未连接/不存在返回 False），提供 `lottery_exists(lottery_id) -> bool`
- [x] **P15-T3**：注册 lottery 校验器——模块加载时 `InteractionResourceValidator.register(InteractionBizTypeEnum.LOTTERY, checker)`，checker 调 `LotteryRpcClient.lottery_exists`；`moment_interaction.thumb`、`favorite`、`moment_publish` 转发 RESOURCE=lottery 时经 validator 校验，不存在 422
- [x] **P15-T4**：动态转发 attach lottery——确认 `moment_publish` RESOURCE 节点（bizType=lottery）已支持；转发 lottery 时校验存在 + 返回新动态
- [x] **P15-T5**：前端 `BiliLotteryCard` 新增点赞/收藏/转发按钮（列表卡片 + 详情页）——点赞调 `POST /moment/thumb`（bizType=lottery）、收藏调 `POST /favorite/add|remove`（bizType=lottery）、转发弹窗调 `createMoment`（RESOURCE=lottery）；**互动状态由容器层 `BiliLotteryCardContainer` 一次性 `GET /moment/interaction-status`（bizType=lottery, bizIds=本页全部 lottery_id）批量拉取后经 `status` prop 下发，卡片 `emit('update-status')` 上报变更**（卡片不再各自 onMounted 查询）；moment-api.ts 薄封装 `thumbMoment`/`favoriteAdd`/`favoriteRemove`/`fetchInteractionStatus`
- [x] **P15-T6**：前端动态 Feed 渲染 lottery 卡片——确认 `MomentContentRenderer` RESOURCE=lottery 渲染 + 跳转抽奖详情；卡片展示点赞/收藏状态与计数
- [ ] **P15-T7**：单元测试——lottery 校验器（存在返回 True / RPC 失败返回 False 放行降级）、点赞/收藏 lottery 校验、转发 RESOURCE=lottery 校验存在、前端交互（点赞/收藏/转发成功刷新状态）
- [x] **P15-T8**：读取动态时 RPC 批量获取 lottery 详情（替代快照）——`CheckLotteryExistRpcResult` 扩展返回 lottery 详情（`title`/`cover`/`jumpUrl`），be-bilibili-crawler `handle_check_lottery_exist` 从 `Lotdata` 回填（title=first_prize_cmt、cover=first_prize_pic、jumpUrl=lottery_detail_url，缺省降级）；be-message `LotteryRpcClient` 新增 `get_lottery_details(lottery_ids) -> dict[int, detail]`（批量一次 RPC）；**Feed 装配管线先收集本页全部动态的 lottery_id 去重后批量回查一次**，再按动态分发填充 desc 模块 RESOURCE=lottery 节点（RPC 失败保留原节点，弱依赖降级）；前端 `LotteryForwardDialog` 转发只传 `bizType/bizId/name`，**不再存 cover/jumpUrl 快照**

**阶段目标**：抽奖卡片在列表与详情页支持点赞、收藏、转发到动态（作为 attach 卡片）；lottery 资源存在性经跨服务 RPC 校验；动态 Feed 正确渲染 lottery 卡片并可跳转抽奖详情；读取时经 RPC 实时获取 lottery 详情，不依赖数据库快照。

### Phase 16：Feed 推荐排序 EdgeRank（2.27.0，计划书决策 #3「后续加推荐算法」落地）

> **方案**：引入经典 Facebook EdgeRank 公式对 Moment Feed / 话题 Feed / 话题广场做推荐排序，
> 与既有时间倒序（`sort=time`）并存，`sort=recommend`（综合 Feed）与 `sort=hot`（话题 Feed）走推荐。
> 无表结构变更、无新增索引；计数一律直接读 `TMomentStat` 字段（禁热路径 COUNT），
> 排序在应用层完成（候选集 + 批量 IN 装配 + 打分）。
>
> **EdgeRank 公式**：
> ```
> score = (w_like·likeCount + w_comment·commentCount + w_repost·repostCount
>          + w_view·viewCount + w_favorite·favoriteCount) × decay(age)
> decay(age) = 0.5 ** (age_seconds / half_life_seconds)
> ```
> - `w_*`：各互动类型权重（由 `settings.edgerank_*_weights` 配置，JSON 字典，可环境变量覆盖）；
> - `half_life_seconds`：半衰期（`settings.edgerank_*_half_life_seconds`），越大衰减越慢、越偏「热度」，
>   越小越偏「新鲜」；
> - `decay`：指数时间衰减，age 以动态 `pubTime` 距今秒数计。
>
> **三套 Profile（权重不同，这正是「综合 Feed 与话题下 Feed 权重不一样」的核心）**：
>
> | Profile | 用途 | 权重取向 | 半衰期 |
> |---|---|---|---|
> | `FEED_PROFILE` | 综合 Feed `/feed/all?sort=recommend` | 侧重内容质量 + 传播性（转发 2.0 > 评论 1.5 > 收藏 1.2 > 点赞 1.0 > 浏览 0.1） | 24h（内容保鲜期较长） |
> | `TOPIC_FEED_PROFILE` | 话题 Feed `/feed/topic/{id}?sort=hot` | 侧重话题讨论氛围 + 热点时效（评论 1.8 > 转发 1.5 > 点赞 1.2 > 收藏 1.0 > 浏览 0.05） | 6h（话题热点降温快） |
> | `TOPIC_SQUARE_PROFILE` | 话题广场 `/topic/square` + 热搜 `/topic/hot-search` | `score = Σ(w·log(count+1))·decay(pubTime)`（dynCount/viewCount 用 log 压缩长尾，isHot/sortWeight 直接加权） | 12h |
>
> **候选集策略（性能约束）**：
> - 综合 Feed `recommend`：候选 = 最近 `edgerank_candidate_window_hours`（默认 72h）内 `auditStatus='normal'`
>   且未软删的动态，`LIMIT edgerank_candidate_limit`（默认 300）条（走既有 `idx_dynamic_pubtime_visible`），
>   批量一次 IN 取 `TMomentStat` + 评论计数（`_load_comment_counts`），应用层打分后按 `score` 倒序、offset 分页；
> - 话题 Feed `hot`：沿用现有「3 倍候选 + 内存排序」骨架，仅把排序键由「like+comment+repost 求和」
>   替换为话题 Profile 打分（候选已按 pubTime 倒序取 3 倍，无需时间窗）；
> - 话题广场：一次 SQL 按打分表达式排序（无聚合子查询），offset 分页。
>
> **分页 / 游标语义（recommend 模式下的已知权衡）**：
> - 推荐排序非按 dynId 有序，`updateBaseline`/`historyOffset` 的 dynId 游标语义在 `recommend` 下**弱化**为
>   近似（返回本页首末 dynId 供前端展示，`history_offset` 参数在 recommend 模式被忽略，翻页走 `page` 偏移）；
> - `time` 模式保持既有 dynId 游标语义完全不变（`history_offset`/`update_baseline` 照旧）。
>
> **与既有能力的关系**：
> - `/feed/following`（关注流）与 `/feed/space/{mid}`（空间页）**不做** EdgeRank，保持既有排序（关注流按时间、
>   空间页置顶+时间），推荐只作用于「公域」综合 Feed / 话题 Feed / 话题广场；
> - 匿名访问（未登录）同样走 `recommend`（打分不依赖用户个性化，属**非个性化** EdgeRank；个性化亲和度
>   因子 `a_e` 作为后续迭代扩展点，本期不引入，无新增表/字段）。

- [x] **P16-T1**：新增 `app/services/edgerank.py` 通用打分模块——
  `EdgeRankProfile`（`weights: dict[str, float]` + `half_life_seconds: float`）、
  `decay(age_seconds, half_life)` 纯函数、`compute_moment_score(counts, pub_time, profile, now=None) -> float`、
  `compute_topic_score(topic, profile, now=None) -> float`；预置 `FEED_PROFILE` / `TOPIC_FEED_PROFILE` /
  `TOPIC_SQUARE_PROFILE`（从 `settings` 读取权重与半衰期，`edgerank_enabled=False` 时 `compute_*` 直接返回
  时间倒序等价分 `1e18 - age_seconds`，保证降级可用）
- [x] **P16-T2**：`app/core/config.py` 新增配置——`edgerank_enabled`（默认 True）、
  `edgerank_feed_weights` / `edgerank_feed_half_life_seconds`（24h）、
  `edgerank_topic_feed_weights` / `edgerank_topic_feed_half_life_seconds`（6h）、
  `edgerank_topic_square_weights` / `edgerank_topic_square_half_life_seconds`（12h）、
  `edgerank_candidate_limit`（300）、`edgerank_candidate_window_hours`（72h）
- [x] **P16-T3**：综合 Feed `MomentFeedService.comprehensive_feed` 新增 `sort` 参数（`recommend` 默认 / `time`）——
  `recommend` 走候选集 + EdgeRank 打分 + offset 分页，`time` 保持原 pubTime 倒序 + dynId 游标；
  `app/api/moment_feed.py` 的 `/feed/all` 新增 `sort` Query 参数
- [x] **P16-T4**：话题 Feed `MomentFeedService.topic_feed` 的 `sort=hot` 排序键由「like+comment+repost 求和」
  替换为 `TOPIC_FEED_PROFILE` EdgeRank 打分（候选骨架不变：3 倍 pubTime 倒序 + 批量 stats + 内存排序）
- [x] **P16-T5**：话题广场 `MomentTopicService.topic_square` 排序改为 `TOPIC_SQUARE_PROFILE` EdgeRank
  （`isHot/sortWeight` 直接加权 + `log(dynCount+1)/log(viewCount+1)` 压缩 + `decay(pubTime)` 时间衰减），
  `hot_only=True`（热搜）与 offset 分页语义不变；`/topic/square` 与 `/topic/hot-search` 自动生效（无 API 变更）
- [x] **P16-T6**：单测 `tests/test_edgerank.py`——公式纯函数（decay 单调递减 / 半衰期行为 / 权重相对大小影响排序 /
  `edgerank_enabled=False` 降级）、综合 Feed `recommend` 排序与分页、话题 Feed `hot` 排序（EdgeRank 替代求和）、
  话题广场排序（dynCount/viewCount 加权 + 时间衰减）

**阶段目标**：综合 Feed / 话题 Feed / 话题广场三处公域排序引入可配置的 EdgeRank 推荐；综合 Feed 与话题 Feed
权重独立可调（满足「话题下 Feed 权重不一样」）；`sort=time` 完整保留可回退；无表结构变更，性能可控。

### Phase 24：收藏夹封面「大小控制 + 先审后发」（2.28.0，对齐头像审核模式）

> **方案**：收藏夹封面与头像同属「用户提交的图片 URL」，复用既有「下载校验 + 先审后发」模式（决策 #31）：
> ① 提交封面（创建/更新收藏夹携带 `coverUrl`）先经 `verify_avatar_url` 下载校验（http/https、1s 内下载、≤1MB、image/*，失败 422）；
> ② 校验通过后**不直接写入 `TFavoriteFolder.cover_url`**，插入 `TFolderCoverAudit` pending 记录（旧 pending 置 rejected，同夹至多一条 pending）；
> ③ 审核通过 → 同事务写 `cover_url` 生效 + 系统通知；驳回 → 保持原封面 + 系统通知（附原因）；
> ④ 管理端新增审核接口（RootUser）+ 用户侧 `mine` 查询；收藏夹列表响应新增 `coverAuditStatus`。

- [x] **P24-T1**：数据模型——新增 `TFolderCoverAudit` 表（`app/models/db/folder_cover_audit.py`，对齐 `TUserAvatarAudit`：folderId/mid/oldCover/newCover/auditStatus/auditOperatorMid/auditReason/auditedAt + 两个索引）+ 枚举 `FolderCoverAuditStatusEnum`（pending/approved/rejected）+ Alembic 迁移——2026-08-21 完成
- [x] **P24-T2**：封面校验复用——`avatar_check.verify_avatar_url` 新增 `label` 参数（错误文案可定制，默认"头像"），收藏夹封面校验传 label="封面"——2026-08-21 完成
- [x] **P24-T3**：收藏夹服务改造——`FavoriteService.create_folder` / `update_folder` 提交非空 `coverUrl` 时先校验（失败 ValueError）再走审核（不写 `cover_url`，插入/覆盖 pending）；空串清除封面直接清 `cover_url`；`list_folders` 批量回填各夹 `coverAuditStatus`（pending 标记）——2026-08-21 完成
- [x] **P24-T4**：审核服务与接口——新增 `app/services/folder_cover_audit.py`（`FolderCoverAuditService`：submit/pending_list/approve/reject/mine，approve 同事务写 cover_url，通知走 NotifyService）+ `app/api/folder_cover_audit.py`（`GET /list`/`POST /approve`/`POST /reject` RootUser + `GET /mine` CurrentUser，前缀 `/api/v1/favorite/folder/cover/audit`），`main.py` 注册；`FavoriteFolderResp` 新增 `coverAuditStatus`——2026-08-21 完成
- [x] **P24-T5**：清理与单测——`cleanup_favorite` 注销清理同步删除 `TFolderCoverAudit`；新增 `tests/test_folder_cover_audit.py` 13 项全通过——2026-08-21 完成
- [x] **P24-T6**：前端管理后台「收藏夹封面审核」页——`AdminLayout` 用户管理端新增「封面审核」入口（root，图标 `svgs/audit/ic_audit.svg` 规范化后接入）+ `FolderCoverAuditListView.vue`（列表：申请者/所属收藏夹/旧封面/新封面/提交时间 + 通过/驳回，驳回弹窗填原因；对齐 `AvatarAuditListView`），路由 `ADMIN_USER_FOLDER_COVER_AUDIT`；`moment-api.ts` 薄封装 `fetchFolderCoverAuditList`/`folderCoverAuditApprove`/`folderCoverAuditReject`（hey-api SDK 已含 `folder/cover/audit/*` 三接口）——2026-08-22 完成

### Phase 25：雪花 ID 生成器统一收敛（2.28.2，内部实现重构）

> **方案**：bili-common 新增可配置通用 `SnowflakeIdGenerator`（可配 `timestamp_bits`/`worker_bits`/`sequence_bits`/`time_unit`），分钟级短 ID（31+4+4）与毫秒级 msgkey（41+10+12）统一由其参数化实例化；`MinuteSnowflakeIdGenerator` 保留为兼容特化（签名与位布局不变，对外 ID 数值不变）。消除原实现锁内自旋：序列号耗尽时记录目标时间片、释放锁后锁外等待再重试。

- [x] **P25-T1**：计划书更新——changelog 新增 2.28.2（PATCH）、决策 #32、本阶段——2026-08-21
- [x] **P25-T2**：bili-common 通用生成器实现——`SnowflakeIdGenerator`（位宽/time_unit 可配、序列耗尽锁外等待）+ `MinuteSnowflakeIdGenerator` 兼容特化——2026-08-21
- [x] **P25-T3**：`sharding.py` 毫秒级 msgkey 改用通用类（41+10+12 / millisecond），删除 `MsgKeyGenerator` 重复实现；`parse_timestamp_ms` 改用生成器 `timestamp_shift`——2026-08-21
- [x] **P25-T4**：回归验证——分钟级/毫秒级 ID 数值与位布局不变、序列耗尽锁外等待不持锁、分库路由（`parse_timestamp_ms`/`db_name_of`/`table_name_of`）解析正确——2026-08-21

**阶段目标**：雪花 ID 生成器统一收敛到 bili-common 单一可配置实现，消除两套重复算法；序列号耗尽不再锁内忙等，避免极端流量下持锁自旋阻塞事件循环/其他线程。

### Phase 26：全互动种子脚本（2.29.0，纯 HTTP 接口版）

> **方案**：收敛为**单文件** `scripts/seed_cli.py`——**统一大规模灌数脚本**：**取消「小规模联调 + 大数据灌数」两阶段概念**，合并为一个统一的大规模灌数流程，**所有内容类型都大批量灌入**（动态 → 评论 → 用户级互动 → 消息与管理），全部走真实业务链路（`x-bili-*` 头模拟网关身份；root 审核统一走 `--admin-mid`），用于全链路联调 / 模拟生产环境 / 暴露代码问题 / 性能压测。动态/话题取自 biliopusdb 真实数据（只读），作者/互动者取自 pptr Postgres 真实用户（只读回查，不写 pptr）；原 `seed_all_interactions_via_api.py` / `seed_via_api.py` / `seed_moment_bulk.py` 等冗余脚本已删除。
>
> **覆盖清单（4 大类 17 项）**：
> 1. **动态体系**：① 话题创建 + 审核通过（`POST /moment/topic/create` → `POST /moment/topic/audit/approve`）；② 动态发布 + 审核通过（`POST /moment/create` → `POST /moment/audit/approve`）；③ 点赞（`POST /moment/thumb`）；④ 转发（`POST /moment/repost` + 再次审核）；⑤ 浏览（`GET /moment/interaction/status/{bizId}?bizType=dynamic`，detail 触发浏览 MQ 异步累计 ViewLog）；⑥ 动态举报（`POST /moment/report`）；⑦ 空间置顶（`POST /moment/space/top`）。
> 2. **评论体系**：⑧ 一级评论 + 审核通过（`POST /comment/add` root=0 → `POST /comment/admin/audit` op=pass）；⑨ 楼中楼多级回复（`POST /comment/add` root=根rpid、parent=父rpid）；⑩ 评论点赞/点踩（`POST /comment/action` action=1/2）；⑪ 评论 @提及（`POST /comment/add` 带 at_mids + at_name_to_mid）；⑫ 评论举报（`POST /comment/report`）；⑬ 评论置顶（`POST /comment/top`）。
> 3. **用户级互动**：⑭ 收藏夹体系（`POST /favorite/folder/create` → 封面审核 `POST /favorite/folder/cover/audit/approve`；`POST /favorite/add` 收藏；`POST /favorite/setting` 可见性）；⑮ 关注/拉黑（`POST /message/follow/do`、`POST /message/follow/block`）；⑯ 事件通知（`POST /message/event/report` like/reply/at 三类）；⑰ 系统通知（`POST /message/notify/admin/create` root 发布）。
> 4. **消息与管理类**：⑱ 私信会话与消息（`POST /message/dm/send` 双向互发 + 管理端 `POST /message/dm/admin/audit` op=pass + `POST /message/dm/ack` 已读）；⑲ 通用互动计数（对 lottery 资源点赞 `POST /moment/thumb` bizType=lottery，落 `TInteractionStat`；RPC 校验失败弱依赖降级放行）；⑳ 用户举报与封禁（`POST /report` bizType=user → 管理端 `POST /message/admin/ban`）；㉑ 审核流（头像 `POST /user/user_info/update` avatar 提交 → `POST /user/avatar/audit/approve`；收藏夹封面 `POST /favorite/folder/create` coverUrl → `POST /favorite/folder/cover/audit/approve`）。
>
> **参数**：`--base-url`（默认 http://127.0.0.1:18739）/ `--admin-mid`（默认 11，role=root）/ `--count`（动态条数，默认 **50000**）/ `--users`（真实用户数，默认 5000）/ `--concurrency`（并发，默认 20）；分模块开关 `--skip-moment` / `--skip-comment` / `--skip-interact` / `--skip-message` / `--skip-follow`；`--dry-run`（只打印计划）。
>
> **幂等与容错**：互动明细走接口幂等（点赞重复返回 isLike=True、事件上报重复返回 duplicated=True）；管理端审核仅对**本人账号**造数，避免污染他人数据；RPC 弱依赖（lottery 校验 / 头像下载校验）失败时记录 warning 并继续，不中断整体流程。

- [ ] **P26-T1**：SeedClient 扩展——在 `scripts/seed_cli.py` 内定义 `SeedClient` 并补齐全互动动作封装（topic create/approve、moment report/top、comment add(root=0)/reply(root,parent)/action/report/top、favorite folder create/add/setting/cover-approve、follow do/block、event report、notify admin create、dm send/admin-audit/ack、report user、admin ban、avatar submit/approve、interaction status）
- [ ] **P26-T2**：全互动编排——按 4 大类顺序编排：动态发布与审核 → 评论与楼中楼 → 点赞/收藏/转发/关注/拉黑/事件 → 私信/系统通知/举报/封禁 → 审核流（头像/封面）→ 通用互动计数
- [ ] **P26-T3**：参数与开关——`--count`（默认 50000）/`--users`（默认 5000）/`--concurrency`（默认 20）/`--base-url`/`--admin-mid`；分模块开关 `--skip-moment`/`--skip-comment`/`--skip-interact`/`--skip-message`/`--skip-follow`；`--dry-run`
- [ ] **P26-T4**：运行验证——本地启动 be-message-service 后 `uv run python scripts/seed_cli.py` 一个命令全量跑通，输出各模块统计（动态/评论/点赞/收藏/关注/事件/通知/私信/举报/封禁/审核条数）
- [ ] **P26-T5**：单文件合并——`seed_via_api.py`（全互动）与 `seed_moment_bulk.py`（大数据灌数）合并入单文件 `scripts/seed_cli.py`（`SeedClient` / `seed()` / `run_bulk()` 同文件、不跨模块引用），删除冗余脚本；分模块开关控制分开执行
- [ ] **P26-T6**：统一大规模灌数改造——**取消「小规模联调 + 大数据灌数」两阶段**，合并为统一大规模灌数流程：动态/评论/互动/消息四模块全部大批量灌入（默认动态 5 万、其余按比例放大）；动态/话题取 biliopusdb 真实数据，用户统一取 pptr Postgres；大规模单条失败软降级跳过，私信撤回/删除全流程断言保留响亮报错

**阶段目标**：提供**单文件统一大规模灌数入口** `scripts/seed_cli.py`：一个命令把全部内容类型（动态/评论/收藏/关注/私信/举报/封禁/审核流）都大批量灌入，通过真实业务链路造数并暴露接口/服务层问题；作为全链路联调、回归验证与性能压测的统一造数入口。

### Phase 27：管理端审核列表按状态筛选（可驳回已过审动态，2.30.0）

> **方案**：现状 `GET /api/v1/moment/audit/list` 硬编码 `auditStatus='auditing'`，管理后台只能看到待审核动态，**无法对「失误过审」的已过审（normal）动态执行驳回**。后端 `MomentAuditService.reject` 本就支持任意状态 → `rejected`（含 `normal`，正确处理 FORWARD `repostCount -1` + 写审计流水 + 发 `AUDIT_REJECT` 驳回通知），本阶段补齐**管理端可见入口**：
> ① 审核列表接口新增可选 `auditStatus` query 参数（默认 `auditing`，完全向后兼容），管理端可按状态拉取已过审 / 已驳回动态；
> ② 前端审核页增加状态 Tab（待审核 / 已过审 / 已驳回），已过审 Tab 对 normal 动态提供「驳回」操作（失误过审撤回），已驳回 Tab 提供「通过」操作（恢复上架）。

- [ ] **P27-T1**：`GET /api/v1/moment/audit/list` 新增可选 query 参数 `auditStatus`（枚举 `auditing`（默认）/`normal`/`rejected`/`hidden`，非法值 422）；`MomentAuditService.pending_list` 增加 `audit_status` 参数按状态过滤（默认 auditing，行为与现状一致）
- [ ] **P27-T2**：单测——各状态筛选正确（auditing/normal/rejected 各自返回对应动态）、默认 auditing 兼容旧行为、非法 auditStatus 422、`normal → reject` 后状态正确流转且 FORWARD repostCount -1
- [ ] **P27-T3**：前端 `MomentAuditListView.vue` 状态 Tab（待审核 / 已过审 / 已驳回）切换拉取对应列表；状态列按实际 `auditStatus` 渲染标签（auditing=待审核 warning / normal=已过审 success / rejected=已驳回 danger / hidden=已下架 info）；操作列按状态区分：`auditing` 显示「通过 / 驳回」、`normal` 仅「驳回」、`rejected` 显示「通过」（恢复）
- [ ] **P27-T4**：SDK 更新（hey-api 重新生成 `audit/list` query 参数）后接入前端 `fetchAuditList` 透传 `auditStatus`（用户手动同步后完成）

**阶段目标**：管理后台可查看已过审 / 已驳回动态，支持「失误过审」动态驳回（normal → rejected，自动从 Feed 下架 + 通知作者）与已驳回动态恢复（rejected → normal）。

### Phase 28：雪花 ID 序列号位宽可配（2.31.0，开发/测试灌数扩容）

> **方案**：分钟级短雪花 ID 位布局为 `| 时间戳(分钟) | 4 bits worker | N bits 序列号 |`，总位数恒为 39 bits。默认 `sequence_bits=4`（时间戳 31 bits，每 worker 每分钟最多 16 个）。大数据灌数（`scripts/seed_cli.py` 并发创建动态/话题）速率超过 16/分钟时，生成器序列号耗尽会**锁外等待到下一分钟**（最长 60s），超过 seed 客户端 30s 超时 → `ReadTimeout`。本阶段把 `sequence_bits` 改为**环境变量可配**（`UID_SEQUENCE_BITS` / `MOMENT_ID_SEQUENCE_BITS` / `TOPIC_ID_SEQUENCE_BITS`，默认 4，范围 4~15），开发/测试环境放宽（如 7 → 每分钟 128 个）即可支撑灌数。
>
> **⚠️ 变更位宽会改变对外 ID 数值空间，与已发布 ID 可能重叠（主键冲突）**，仅允许在**清库重建（无历史 ID）**的开发/测试环境启用；生产环境必须保持默认 4。时间戳位宽随 `sequence_bits` 相应缩减（`31-(sequence_bits-4)`），`sequence_bits=7` 时 28 bits 分钟 ≈ 510 年，余量充足。
>
> **配套**：`scripts/seed_cli.py` 客户端超时改为环境变量可配（`SEED_HTTP_TIMEOUT` 默认 90s / `SEED_REQ_TIMEOUT` 默认 120s），覆盖跨分钟等待（≤60s），默认配置下不配位宽也不会再误报 `ReadTimeout`。

- [ ] **P28-T1**：bili-common `MinuteSnowflakeIdGenerator` 增加可选 `sequence_bits` 参数（默认 4 保持兼容，校验 4~15），`timestamp_bits = 39 - 4 - sequence_bits`，总位数恒 39
- [ ] **P28-T2**：`config.py` 新增 `uid_sequence_bits` / `moment_id_sequence_bits` / `topic_id_sequence_bits`（默认 4，环境变量 `UID_SEQUENCE_BITS` / `MOMENT_ID_SEQUENCE_BITS` / `TOPIC_ID_SEQUENCE_BITS`）；`sharding.py` 三个生成器传入对应配置
- [ ] **P28-T3**：`scripts/seed_cli.py` 客户端超时环境变量化（`SEED_HTTP_TIMEOUT` / `SEED_REQ_TIMEOUT`，默认 90s / 120s）
- [ ] **P28-T4**：回归验证——默认 `sequence_bits=4` 时 ID 数值与位布局不变（既有测试全绿）；`sequence_bits=7` 时单 worker 每分钟容量提升至 128、ID 为正数、位布局符合预期

**阶段目标**：开发/测试环境可通过环境变量放宽分钟级短雪花 ID 容量，消除 seed 灌数在分钟边界因序列号耗尽导致的 `ReadTimeout`；生产保持默认 16/分钟容量与既有 ID 数值空间不变。

| 里程碑 | 完成标志 |
|---|---|
| M1 | Phase 1 完成：数据库评审通过，迁移脚本执行无报错 |
| M2 | Phase 3 完成：可完整发布（auditing）+ 手动改 normal → Feed + 详情浏览闭环 |
| M3 | Phase 5 完成：辅助功能（话题/@/LBS）开发完成，进入审核 & 事件阶段 |
| M4 | Phase 6 完成：审核流 + 事件联动开发完成，进入联调测试 |
| M5 | Phase 7 完成：功能验收通过，可部署上线 |
