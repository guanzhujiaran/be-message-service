[← 返回目录](./README.md)

## 一、数据模型分析总结（基于 B站 Proto 反向推导）

### 1.1 数据来源

基于 `be-bilibili-crawler/Service/GrpcModule/Grpc/GrpcProto/bilibili/` 下的以下 proto 文件：

- [dynamic/common/dynamic.proto](file:///home/minato/BilibiliExplosion/be-bilibili-crawler/Service/GrpcModule/Grpc/GrpcProto/bilibili/dynamic/common/dynamic.proto) — Moment通用模型（Opus、Paragraph、CreateContent、CreateScene 等）
- [dynamic/gw/gateway.proto](file:///home/minato/BilibiliExplosion/be-bilibili-crawler/Service/GrpcModule/Grpc/GrpcProto/bilibili/dynamic/gw/gateway.proto) — Moment网关模型（DynamicItem、Module、MdlDyn* 系列）
- [dynamic/interfaces/feed/v1/api.proto](file:///home/minato/BilibiliExplosion/be-bilibili-crawler/Service/GrpcModule/Grpc/GrpcProto/bilibili/dynamic/interfaces/feed/v1/api.proto) — Moment Feed 接口（CreateDyn、DynamicRepost、DynamicThumb、RmDyn 等）
- [app/dynamic/v2/dynamic.proto](file:///home/minato/BilibiliExplosion/be-bilibili-crawler/Service/GrpcModule/Grpc/GrpcProto/bilibili/app/dynamic/v2/dynamic.proto) — V2 Moment服务（DynAll、DynDetail、DynSpace、DynThumb 等）

### 1.2 核心实体关系

```
TMoment (Moment 主表)
  ├── TMomentStat     (统计数据：点赞/评论/转发/浏览 — 明细表 + 原子 ±1 双写)
  ├── TMomentTopic    (话题主数据：话题广场 / 审核；对外 topicId 雪花 ID)
  ├── TMomentTopicRel (动态-话题多对多关系，2.22.0；TMoment.topicId=主话题=topics[0])
  ├── TMomentAuditLog (审核流转记录)
  ├── TMomentLike     (点赞明细 — 幂等 + 计数；2.17.0 起泛化支持 bizType+bizId)
  ├── TMomentViewLog  (浏览去重明细 — 幂等 + 计数)
  ├── TMomentReport   (举报)
  ├── TCommentIndex    (评论区：复用现有评论系统，CommentTypeEnum.DYNAMIC)
  └── TEventFeed       (事件提醒：点赞/评论/@/审核驳回)

收藏 / 点赞多业务资源（2.17.0 泛化）
  ├── TMomentFavorite (收藏明细 — 泛化支持 bizType+bizId，动态时 bizId=dynId)
  ├── TInteractionStat (通用交互计数：仅承载非动态资源 likeCount/favoriteCount)
  └── TFavoriteFolder / TUserFavoriteSetting (收藏夹与可见性，不变)
```

> **2.17.0 泛化说明**：收藏（`TMomentFavorite`）与点赞（`TMomentLike`）明细表从「仅绑定动态 `dynId`」泛化为「任意业务资源 `bizType` + `bizId`」。资源类型由 `InteractionBizTypeEnum` 注册式定义（`dynamic` / `lottery` 抽奖卡片 / `rpa_action` RPA自定义操作 / `rpa_workflow` RPA工作流 / `rpa_browser` RPA浏览器实例），新增类型无需改表。动态资源的计数仍走 `TMomentStat`；非动态资源的计数走新增通用表 `TInteractionStat`（`(bizType,bizId)` 联合主键）。
>
> **2.21.0 attach 卡片重构**：动态引用外部资源（抽奖卡片 / RPA 资源等）的 attach 卡**不再写入正文 `contentJson` 的 RESOURCE 节点**（2.17.0-2.20.1 的旧方案，会在正文内联卡片并冗余存 name/cover），改为复用 `TMoment.bizType` + `TMoment.bizRid` 两列**只落 `bizType` + `bizId`**（对标 B 站 `CreateCommonAttachCard { type, biz_id }`），读取 Feed/详情时装配为**独立 `moduleType="additional"` 模块**（对标 B 站 `DynModuleType.module_additional` / `ModuleAdditional { type, rid }`），渲染在 desc 正文**下方**；卡片的 name/cover/jumpUrl 由装配层按 `bizType` 批量经 RPC 实时获取（lottery → `LotteryRpcClient`），弱依赖失败仅返回 bizType+bizId。

### 1.3 Moment 类型枚举（MVP 仅 WORD + FORWARD，对齐 B站 DynamicType）

| 值 | 类型 | MVP 支持 | 说明 |
|---|---|---|---|
| 1 | `FORWARD` | ✅ | 转发Moment |
| 2 | `AV` | ❌ | 稿件/视频Moment（后续迭代） |
| 3 | `PGC` | ❌ | 番剧/PGC（后续迭代） |
| 6 | `WORD` | ✅ | 纯文字Moment（正文富文本中可插入外站图片 URL 链接） |
| 7 | `DRAW` | ❌ | 图文Moment（后续迭代；当前图片走 WORD 正文外链 URL） |
| 8 | `ARTICLE` | ❌ | 专栏Moment（后续迭代） |
| 12 | `LIVE` | ❌ | 直播Moment（后续迭代） |
| 16 | `APPLET` | ❌ | 小程序卡（后续迭代） |
| 18 | `LIVE_RCMD` | ❌ | 直播推荐卡（后续迭代） |

> **MVP 图片方案**：不支持图片上传到服务端；用户只能在文字Moment正文的富文本节点中插入**外站图片 URL 链接**（`ParagraphType.LINK + jumpUrl=图片地址`），由前端决定渲染方式。服务端仅保存为普通 `contentJson` 富文本节点，不做下载/存储/鉴真。

### 1.4 审核流程

```
  用户发布 → auditStatus = 'auditing'（Feed/详情对普通用户不可见，仅作者本人空间可见「审核中」标签）
     │
     ├─→ 管理员审核通过 → auditStatus = 'normal' → 进入 Feed，全量可见（不发通知）
     │
     └─→ 管理员审核驳回 → auditStatus = 'rejected' → 写 TMomentAuditLog + 发驳回通知给作者（含驳回原因）
              │
              └─→ 用户可以选择：① 修改正文后重新提交（回到 auditing） ② 删除Moment
```

### 1.5 模块结构（对齐 B站 DynModuleType）

每条Moment由多个模块组成，前端按模块顺序渲染：

- `module_author` → 发布人信息（头像、昵称、关注按钮、发布时间）
- `module_desc` → 描述文案（富文本/@/表情/话题/外链图片 URL）
- `module_dynamic` → 正文卡（文字/转发嵌套）
- `module_forward` → 转发嵌套（源Moment 卡片）
- `module_extend` → 小卡（话题/LBS 标签；2.22.0 起支持**多话题** `topics[]`，渲染于作者信息与正文之间）
- `module_additional` → **附加卡（2.21.0）**：动态下方引用的外部资源卡片（抽奖卡片 / RPA 资源等），只存 `bizType`+`bizId`，name/cover/jumpUrl 由装配层 RPC 实时获取；渲染于 desc 正文下方（对标 B 站 `module_additional`）
- `module_stat` → 统计（点赞/评论/转发数）
- `module_interaction` → 外露交互（点赞/评论入口）
