"""消息服务。

按消息类型拆分为以下子包，调用方按需从对应子包导入：

| 子包            | 职责（站内 / 站外）                                          |
| --------------- | ------------------------------------------------------------ |
| `dm`            | 站内私信（单聊）：写扩散发送、会话列表、聊天记录、删除撤回   |
| `insite`        | 站内消息系统：站内信通知 / 事件提醒 / 消息设置 / 活跃度       |
| `external`      | 站外渠道推送（PushMe / PushPlus / SMTP 等）发送实现           |
| `infrastructure`| 统一的 MQ 投递封装（publisher，站内 / 站外共用）              |

评论（写 / 读 / 互动 / 审核 / 审计）已移出本服务，见 `app.services.comment`。

说明：站内消息系统（insite）在落库 / 推送前会读取 `insite.setting` 中的用户设置
（recv_like / recv_reply / recv_at / recv_notify / recv_stranger_dm）作为第一道闸门；
站外渠道推送（external）是独立于站内信的第三方提醒，不读取上述站内设置。
"""
