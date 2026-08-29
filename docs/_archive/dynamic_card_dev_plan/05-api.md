[← 返回目录](./README.md)

## 五、API 接口规范

所有接口统一放在 `/api/v1/moment/` 下，响应格式对齐现有 be-message 规范（`code`/`msg`/`data`），鉴权复用现有 `get_current_user` 依赖（JWT → `x-bili-mid`）。

### 5.1 发布类接口

| 方法 | 路径 | 说明 | 请求体关键字段 |
|---|---|---|---|
| POST | `/create` | 创建Moment | `scene`(WORD/FORWARD), `content`(富文本，支持外链图片 URL 节点), `attach?{bizType,bizId}`(2.21.0 附加卡，只存 bizType+bizId，读取时 RPC 实时获取详情), `repostSrc{dynId}`, `topics?[{topicId}]`(2.22.0 多话题，去重、最多 5 个，仅可关联 `auditStatus='normal'` 话题；兼容 `topic` 单话题字段，二者合并去重), `lbs`, `option{closeComment}` → 响应 `auditStatus='auditing'` |
| POST | `/edit` | 编辑Moment | `dynId`, `scene`, `content`, `attach?`, `topics?`, `option`；rejected/auditing 编辑后自动回 auditing |
| POST | `/remove` | 删除Moment（软删） | `dynId` → 设置 `deletedAt`；**仅作者本人**（`dyn.mid == mid` 否则 400），已删幂等返回；FORWARD ∧ before=normal → 源Moment `repostCount -1`（状态机触发点④）；AuditLog operatorRole=author |
| POST | `/admin/remove` | 管理员删除Moment（软删，2.22.1） | `dynId`；**仅 root**（`RootUser`，与 `moment/audit/*` 权限一致），跳过作者校验可删除**任意动态**；软删语义/状态机触发点④ 与 `/remove` 一致，AuditLog `operatorRole=admin` + `remark='管理员删除'` |
| POST | `/repost` | 转发Moment（FORWARD） | `srcDynId`(源Moment必须 auditStatus='normal'), `content`(转发语) |
| POST | `/space/top` | 空间置顶 | `dynId`（仅本人，且Moment auditStatus='normal'） |
| POST | `/space/untop` | 取消置顶 | `dynId` |
| POST | `/create/check` | 发布页预校验 | `scene` → 返回 `{setting, permission, allowedScenes:['WORD','FORWARD']}` |

### 5.2 Feed 流接口

| 方法 | 路径 | 说明 | 查询参数 |
|---|---|---|---|
| GET | `/feed/all` | 综合页 Feed（仅 normal+未软删） | `sort`（`recommend`=EdgeRank 推荐，默认 / `time`=pubTime 倒序）, `updateBaseline`, `historyOffset`, `page`, `refreshType(1=刷新,2=翻页)` |
| GET | `/feed/space/{mid}` | 个人空间 Feed | `hostMid`, `offset`, `page`, `isPreload`；**本人请求**返回 auditing/rejected 带状态标签；**访客请求**过滤为仅 normal |
| GET | `/feed/topic/{topicId}` | 话题 Feed 流 | `topicId`, `offset`, `page`（仅 normal） |
| GET | `/detail/{dynId}` | Moment 详情 | `dynId`；本人可看全部状态；访客仅 normal（或返回 404/审核中占位卡） |
| POST | `/details` | 批量Moment 详情 | `dynamicIds[]`（逗号分隔，限 20 条；按请求者权限过滤） |

> **注意**：所有对外 Feed/详情接口的 WHERE 必须包含 `deletedAt IS NULL`，并按请求者身份决定 auditStatus 过滤条件。视频页 Feed（`/feed/video`）MVP 不实现，后续 DRAW/AV 迭代再开。

**Feed 分页游标方案（对齐 B站）：**

- `updateBaseline`：刷新基线（最新一条 dynId），用于下拉刷新时的"更新了 N 条"
- `historyOffset`：历史偏移（最旧一条 dynId + 时间戳编码），用于上拉加载
- `hasMore`：布尔值，是否还有下一页
- `updateNum`：刷新后新增条数

### 5.3 互动接口

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/thumb` | 点赞/取消点赞（`up: 1=赞, 2=取消`；仅 normal Moment允许） |
| POST | `/report` | 举报Moment（`reasonType`, `reasonDesc?`） |
| POST | `/attach-card/button` | 附加卡按钮点击（MVP 不实现，返回 501 占位） |
| POST | `/vote` | Moment投票操作（MVP 不实现，同上占位） |

> **浏览计数无独立上报接口**：浏览量由后端在动态详情接口 `GET /api/v1/moment/detail/{moment_id}` 被真实访问时自动累计（仅登录用户，按 `mid+dynId+refDate` 去重），前端无需也无法主动上报。

**评论举报接口（新增，路由前缀 `/api/v1/comment`）：**

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/report` | 举报评论（`rpid`, `reasonType`(复用 `MomentReportReasonEnum`), `reasonDesc?`；一人一条去重；累计达 `comment_report_threshold`(默认3) 时评论 `state` 由 `normal` → `auditing`） |

**收藏夹系统接口（路由前缀 `/api/v1/favorite`，写入需登录 `RequiredUser`；2.17.0 起 `bizType` 默认 `dynamic`，`bizType≠dynamic` 时 `bizId` 必填）：**

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/folder/create` | 创建收藏夹（`name`, `description?`, `coverUrl?`）。**2.28.0 起 `coverUrl` 先下载校验（复用头像校验：http/https、1s 内下载、≤1MB、image/*，失败 422）后进入封面审核（pending），不直接写入 `cover_url`**；响应新增 `coverAuditStatus`（有 pending 时 "pending"，否则 null） |
| POST | `/folder/update` | 更新收藏夹（`folderId`, `name?`/`description?`/`coverUrl?`）。**2.28.0 起非空 `coverUrl` 同样先校验后进入封面审核（pending）**；空字符串表示清除封面（直接清 `cover_url`，不经审核） |
| POST | `/folder/delete` | 删除收藏夹（`folderId`，连其下收藏，favoriteCount 按用户去重回退） |
| GET | `/folder/list` | 我的收藏夹列表（含各夹收藏数）。**2.28.0 起每项新增 `coverAuditStatus`**（该夹存在 pending 封面审核时 "pending"，否则 null） |

**收藏夹封面审核接口（路由前缀 `/api/v1/favorite/folder/cover/audit`，2.28.0 新增，对齐头像审核范式）：**

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/list` | 管理员待审核封面队列（`RootUser`；`page_num`, `page_size`；仅 `auditStatus=pending` 按创建倒序；项含 `pk/folderId/mid/authorName/oldCover/newCover/createdAt`） |
| POST | `/approve` | 审核通过（`RootUser`；body `{ pk, remark? }`）：状态→approved + **同事务**写 `TFavoriteFolder.cover_url` 生效 + 系统通知用户（通过） |
| POST | `/reject` | 审核驳回（`RootUser`；body `{ pk, reason, remark? }`）：状态→rejected + 保持原封面 + 系统通知用户（附驳回原因） |
| GET | `/mine` | 我的某收藏夹封面审核状态（`CurrentUser`；query `folderId`）：返回该夹最近一条 `{ pk, folderId, newCover, oldCover, auditStatus, auditReason, createdAt, auditedAt }`，无记录 `data=null` |
| POST | `/add` | 收藏资源到收藏夹（`bizId`, `bizType?`(默认 dynamic), `folderId`；幂等，favoriteCount 用户去重 +1） |
| POST | `/remove` | 从收藏夹取消收藏（`bizId`, `bizType?`, `folderId`；用户无其它夹收藏时 favoriteCount -1） |
| GET | `/list` | 我的某收藏夹下资源 id 列表（`folderId`, `bizType?`, `page`, `pageSize`；不带 bizType 返回全部类型） |
| GET | `/items` | **2.17.0 新增**：我的某收藏夹下资源明细（`folderId`, `page`, `pageSize`；返回 `bizType`+`bizId` 对列表，前端可按类型渲染） |
| GET | `/dyn/folders` | 某资源被我收藏在哪些收藏夹（`bizId`, `bizType?`） |
| GET | `/setting` | 我的主页收藏可见性（`showFavorites`） |
| POST | `/setting` | 设置主页收藏可见性（`showFavorites`） |
| GET | `/user/folders` | **公开读（无需登录）**：某用户主页公开收藏夹列表（`mid`；该用户 `showFavorites=0` 时返回 403） |
| GET | `/user/dynamics` | **公开读（无需登录）**：某用户某收藏夹下公开资源（`mid`, `folderId`, `page`, `pageSize`；不公开/夹不存在返回 403） |

**点赞/互动接口泛化（路由前缀 `/api/v1/moment`；2.17.0 起支持多资源）：**

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/thumb` | 点赞/取消点赞（`bizId`, `bizType?`(默认 dynamic), `up`；动态仅 normal 可赞；非动态资源经 `InteractionResourceValidator` 校验存在性） |
| GET | `/interaction/status` | **2.17.0 新增**：批量查询某资源当前用户收藏/点赞态 + 计数（`bizType`, `bizIds[]`；返回每资源 `isLike`/`likeCount`/`isFavorite`/`favoriteCount`/`viewCount`）；**列表专用，不累计浏览**；**2.23.1 防乱调**——返回前对 `bizIds` 逐个校验资源存在性：dynamic 本地查 `TMoment`（deletedAt 非空 / 非 normal 视为不存在）、lottery 批量 RPC 校验（RPC 失败弱依赖降级放行）、其余类型放行；**存在缺失的资源则整体返回 400 并列出不存在的 bizId**（不返回部分结果），杜绝伪造资源 id 乱刷互动态 |
| GET | `/interaction/status/{bizId}` | **2.23.1 新增**：单资源互动状态（**detail 页专用**，兼作浏览统计触发点）——同字段返回单个 `InteractionStatusItem`；校验资源存在（缺失 400）；**查询后投递 `InteractionViewPayload` 到 `interaction.view` 队列异步累计浏览**（动态走 `TMomentViewLog`+`TMomentStat.viewCount`，非动态走 `TInteractionViewLog`+`TInteractionStat.viewCount`），仅真实 detail 页访问才 +1，批量列表调用不累计 |

**跨服务资源详情（2.18.0 RPC 方案）：**

非动态资源（lottery / rpa_action / rpa_workflow / rpa_browser / rpa_plugin）的明细与计数存于 be-message，
但其**资源实体详情**（名称、封面、作者、跳转地址等）归属各业务系统（RPA-Browser / be-bilibili-crawler）。

- **存储边界**：be-message 只存「互动明细 + 计数」（`TMomentLike` / `TMomentFavorite` / `TInteractionStat`），
  不冗余存资源详情，避免多服务数据不一致；
- **RPC 获取详情**：当接口需要随互动状态返回非动态资源详情时，be-message 作为 **RPC 客户端**，
  通过 bili-common 统一契约调用资源归属服务（如 RPA-Browser）暴露的 **RPC 服务端**方法
  `get_resource_detail(biz_type, biz_id)`，获取详情后随 `interaction/status` 等接口一并返回；
- **RPC 契约**：`bili-common/rpc/base.py` 注册路由键（`message.rpa.rpc.*` 等）与方法名、参数/响应模型，
  参数 `{ bizType, bizId }`，响应 `{ name, cover, authorMid, jumpUrl, extra? }`（按类型扩展）；
- **降级策略**：RPC 超时 / 资源不存在时，接口仍返回互动态 + 计数，资源详情字段置空，
  不影响前端互动主流程（弱依赖）。

### 5.4 话题 & @ & LBS 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/at/list` | @用户推荐列表（最近联系/关注/粉丝分组） |
| GET | `/at/search` | @用户搜索（keyword 模糊匹配昵称） |
| GET | `/topic/square` | 话题广场列表（**仅 `auditStatus='normal'`**，2.27.0 起按话题 EdgeRank 排序：`Σ(w·log(count+1))·decay(pubTime)`，含 isHot/sortWeight/dynCount/viewCount 加权；权重配置 `settings.edgerank_topic_square_*`） |
| GET | `/topic/hot-search` | 热门话题搜索（**仅 `auditStatus='normal'`**，2.27.0 起同话题广场 EdgeRank 排序，`hot_only=True` 过滤 isHot=1） |
| GET | `/topic/mine` | 我创建的话题（含全部审核状态，`CurrentUser`；每话题返回 auditStatus/auditRejectReason/pubTime） |
| POST | `/topic/create` | 创建话题（`CurrentUser`）：`topicName`(必填,1-30), `topicCover`?(http/https URL), `topicDesc`?(≤200)；名称**唯一**校验；创建即 `auditStatus='auditing'`，不公开展示 |
| GET | `/poi/nearby` | 附近地点（lat, lng, page） |
| GET | `/poi/search` | POI 关键词搜索（keyword, lat, lng） |

> **话题 Feed**：`GET /feed/topic/{topicId}`（见 5.3 节）仍仅展示 `auditStatus='normal'` 的动态；若目标话题 `auditStatus≠normal`，话题 Feed 返回空/404（游客与作者均不可见）。

### 5.4.1 话题审核管理接口（管理员权限守卫，路由前缀 `/api/v1/moment/topic/audit`，2.19.0 起）

| 方法 | 路径 | 说明 | 请求体/参数 |
|---|---|---|---|
| GET | `/list` | 管理员话题待审核列表 | `page`, `pageSize`；默认 WHERE `auditStatus='auditing'`，按 `createdAt DESC`，回查创建者 uname/face |
| POST | `/approve` | 审核通过 | `topicId`, `remark?` → `auditStatus='normal'` + `pubTime=now()`；**不发通知** |
| POST | `/reject` | 审核驳回 | `topicId`, `rejectReason` → 写 `auditRejectReason` + `auditStatus='rejected'`；**发驳回通知给创建者**（对齐动态 P6-T3） |

### 5.5 审核管理接口（管理员权限守卫，路由前缀 `/api/v1/moment/audit`）

| 方法 | 路径 | 说明 | 请求体/参数 |
|---|---|---|---|
| GET | `/list` | 管理员审核列表（**2.30.0 起支持按状态筛选**） | `page`, `pageSize`；`auditStatus?`（可选，`auditing`（默认）/`normal`/`rejected`/`hidden`，非法值 422；默认 WHERE `auditStatus='auditing'` 保持向后兼容），按 `createdAt DESC`。`auditStatus='normal'` 用于拉取已过审动态并执行「驳回」（失误过审撤回） |
| GET | `/list/history` | 审核历史流水 | `dynId?`, `operatorMid?`, `actionType?`, `fromDate`, `toDate`, `page` |
| POST | `/approve` | 审核通过 | `dynId`, `remark?` → 写 `TMomentAuditLog(action=approve)`；**不发通知**；dynType=FORWARD 时 srcDyn.repostCount +1 |
| POST | `/reject` | 审核驳回 | `dynId`, `rejectReason` → 写 `auditRejectReason` + 审核日志 + **发驳回事件通知给作者**；FORWARD 且 before=normal 时 srcDyn.repostCount -1。**支持从任意状态驳回（含 normal 已过审动态）** |
| GET | `/{dynId}` | 单条Moment 审核详情（含全部状态 + 历史流转） | `dynId` |

### 5.6 关注流接口

| 方法 | 路径 | 说明 | 查询参数 |
|---|---|---|---|
| GET | `/feed/following` | 关注流 Feed（仅展示**我关注的人**发布的 normal 动态） | `page`, `page_size`, `history_offset`；**必须登录**（RequiredUser，未登录 401） |

> 关注 mid 集合取自 `msg_user_follow`（`FollowService.list_following_mids` 全量拉取，按关注时间倒序）；
> 动态过滤与综合页一致（normal + 未软删 + pubTime 非空），按 pubTime 倒序，复用综合页装配管线。
> 对标 B 站首页「关注」Tab：只展示当前用户关注对象发布的动态。

### 5.7 空间统计接口（对标 B 站 upstat）

| 方法 | 路径 | 说明 | 查询参数 | 响应 |
|---|---|---|---|---|
| GET | `/upstat` | 空间统计（动态数 / 获赞数） | `vmid`（目标用户 mid） | `MomentUpStatResp { mid, dynamic_count, like_count }` |

> 对标 B 站 `https://api.bilibili.com/x/space/upstat?mid=`，返回指定用户对外可见 Moment 的**动态总数**与**获赞总数**（后者为这些 Moment 的 `TMomentStat.likeCount` 求和）。
> 属低频统计接口，允许 `COUNT`/`SUM` 聚合（与请求热路径的「明细表 + 原子 ±1」范式不冲突，该范式仅约束点赞/浏览等高频计数）。

### 5.8 响应示例（Moment 详情 — WORD 型 + 外链图片节点）

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
    "extend": { "topicId": 123, "topicName": "风景" }
  }
}
```

> **extend 模块（话题）字段说明**（2.10.0 起补齐 `topicName`；2.22.0 起支持多话题 `topics`）：

| 字段 | 类型 | 说明 |
|---|---|---|
| `topicId` | int | 主话题 ID（= `topics[0]`，兼容存量单话题客户端） |
| `topicName` | string | 主话题名称（取自 `TMomentTopic.topicName`，装配层批量回填；话题已删除/不存在时为 `null`，前端兜底显示 `#话题 {topicId}#`） |
| `topics` | array | **（2.22.0）** 动态关联的全部话题 `[{topicId, topicName}]`，按关联顺序排列；装配层经 `TMomentTopicRel` 批量回填名称（无 N+1）；存量数据仅 `TMoment.topicId` 有值且关系表无行时，`topics=[{topicId, topicName}]` 单元素兜底 |

> **多话题装配约定**：Feed/详情装配时，先批量查询 `TMomentTopicRel` 取全部 dynId 的话题关系，再按 `dynId` 分组回填 `topics`；对关系表无行但 `TMoment.topicId` 有值的存量数据，以主话题兜底。前端渲染顺序：作者信息（module_author）→ 话题卡（module_extend，**多个话题逐卡渲染**）→ 正文（module_desc）。

### 5.9 用户空间信息接口（对标 B 站 `/x/space/wbi/acc/info`）

> 用户空间信息相关接口（空间 Feed、空间统计、关注统计、空间资料）统一归类到本功能域（2.12.0 新增）。

| 方法 | 路径 | 说明 | 查询参数 | 鉴权 |
|---|---|---|---|---|
| GET | `/user/space/info` | 用户空间完整资料（对标 B 站 `/x/space/wbi/acc/info`） | `mid`（目标用户 mid） | 公开（无需登录；登录时附带 `is_followed`/黑名单判断） |
| GET | `/moment/feed/space/{mid}` | 个人空间动态 Feed | `mid` 路径参数 | 公开（本人=全部状态，访客=仅 normal） |
| GET | `/moment/upstat` | 空间统计（动态数/获赞数） | `vmid` | 公开 |
| GET | `/message/follow/stat` | 关注/粉丝/互关数 | `vmid` | 公开 |

> **（2.32.0）聚合统计字段**：`GET /user/space/info` 的响应**已内联**上述两个统计端点的结果（`follow_stat` / `upstat`）。
> 悬浮用户卡片 / 空间页**应只调 `/user/space/info` 一次**，不再并发调 `/message/follow/stat` + `/community/upstat`（3 次 HTTP + 3 次黑名单判定 → 1 次）。
> 两个原端点**保留不动**（向后兼容，供只需要单项统计的场景使用）。

**接口：`GET /api/v1/user/space/info?mid=<mid>`**

返回 `StandardResponse{code, msg, data}`，`data` 对标 B 站 acc/info 的 `data` 结构：

```json
{
  "code": 0,
  "msg": "success",
  "data": {
    "mid": 123,
    "name": "昵称",
    "sex": "保密",
    "face": "https://.../avatar.jpg",
    "sign": "签名",
    "level": 6,
    "rank": 10000,
    "jointime": 0,
    "moral": 0,
    "silence": 0,
    "coins": 0,
    "birthday": "01-01",
    "official": { "role": 0, "title": "", "desc": "", "type": -1 },
    "vip": { "type": 1, "status": 0, "due_date": 0, "label": { "text": "" } },
    "pendant": null,
    "nameplate": null,
    "top_photo": null,
    "is_followed": false,
    "is_self": false,
    "follow_stat": { "following_count": 12, "follower_count": 34, "mutual_count": 5 },
    "upstat": { "dynamic_count": 7, "like_count": 89 }
  }
}
```

**字段映射与默认值**（数据源 `PptrUserService.get_user_profile` 四表联查）：

| 返回字段 | 数据源 | 说明 |
|---|---|---|
| `mid` | `TUserInfo.uid` | 用户 ID |
| `name` | `TUserDetail.uname` | 昵称 |
| `sex` | `TUserDetail.sex` | 性别 |
| `face` | `TUserDetail.avatar` | 头像 |
| `sign` | `TUserDetail.sign` | 签名 |
| `level` | `TUserLevel.current_level` | 等级 |
| `birthday` | `TUserDetail.birthday` | 生日（`MM-DD`） |
| `vip` | `TUserVip` | VIP 信息（type/status/due_date） |
| `is_followed` | `FollowService.is_following` | 当前登录用户是否关注目标（未登录为 `false`） |
| `official`/`pendant`/`nameplate`/`top_photo` | —（pptr 无数据源） | 固定为空结构/`null` |
| `follow_stat` | `FollowService.get_counts`（2.32.0 内联） | `{following_count, follower_count, mutual_count}`，等价于 `GET /message/follow/stat?vmid=mid` 的计数部分（**不含** `mid` 冗余字段） |
| `upstat` | `MomentFeedService.get_upstat`（2.32.0 内联） | `{dynamic_count, like_count}`，等价于 `GET /community/upstat?vmid=mid` 的统计部分（**不含** `mid` 冗余字段） |

> **（2.32.0）聚合字段语义**：两者均为**只读派生字段**（`default_factory` 提供零值），不计入黑名单判定的额外请求——与 `is_followed` 复用同一个已通过黑名单校验的会话串行补查。原 `/message/follow/stat`、`/community/upstat` 端点语义与响应**完全不变**。

**错误响应**：

| 场景 | `code` | `msg` | 说明 |
|---|---|---|---|
| `mid` 非法（≤0） | `400` | `mid 不合法` | 参数校验 |
| 用户不存在 | `USER_NOT_FOUND`（新增，如 `1008`） | `用户不存在` | **不再**回退为空列表/`0`/`404` 兜底 |
| 与目标存在黑名单关系（登录用户被目标拉黑 或 已拉黑目标） | `403` | `对方已将你加入黑名单，无法访问其空间` | 拒绝返回空间数据 |

> 说明：`GET /space/info`、`feed/space/{mid}`、`upstat`、`follow/stat` 均需做黑名单访问判断（存在 `i_blocked`/`blocked_by` 关系即拒绝），保证黑名单用户在前后端都无法互访空间。

### 5.10 用户个人资料更新接口（含头像修改，2.16.0）

| 方法 | 路径 | 说明 | 请求体 | 鉴权 |
|---|---|---|---|---|
| POST | `/user/user_info/update` | 更新当前登录用户资料（昵称/签名/性别/生日/**头像 URL**） | `{ uname?, usersign?, sex?, birthday?, avatar? }` | 本人（`CurrentUser`） |

**`avatar` 字段（2.16.0 新增）**：

| 字段 | 类型 | 说明 |
|---|---|---|
| `avatar` | string | 图片 URL 链接（仅 `http/https`）；**空串表示不修改头像**。**后端下载校验**：① 1s 内完成下载（超时即拒）；② 文件大小 ≤ 1MB（流式累计超限即拒）；③ 响应 Content-Type 必须为图片（`image/*`）。校验通过后写入 pptr `TUserDetail.avatar` |

**校验失败错误响应**：

| 场景 | `code` | `msg` |
|---|---|---|
| 非 http/https 协议 | `422` | `头像链接仅支持 http/https` |
| 下载超时（>1s）/ 网络错误 | `422` | `头像图片下载超时或网络异常` |
| 文件大小 > 1MB | `422` | `头像图片不能超过 1MB` |
| 非图片 Content-Type | `422` | `头像链接不是有效的图片` |
