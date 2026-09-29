"""消息服务 RPC 服务端模块。

- rpc_pptr_user：pptr 用户读写 RPC（message.pptr.rpc.*）
- rpc_external_push：站外推送 RPC（message.push.rpc.*）
- rpc_notify：系统通知 RPC（message.notify.rpc.*）
- rpc_geoip：IP 属地解析 RPC（message.geoip.rpc.*，复用本服务的 GeoLite2 mmdb）
"""
