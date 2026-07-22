# astrbot_plugin_agent_completion_notifier

独立的 AstrBot 开发工具结束通知插件。接收 Codex/OpenCode 一轮结束事件，并通过已绑定的
AstrBot 会话主动推送到 QQ。它不依赖 CUGB 校园助手插件。

## 服务端配置

```json
{
  "owner_qq_id": "你的 QQ 号",
  "enabled": true,
  "host": "127.0.0.1",
  "port": 8766,
  "path": "/api/hooks/agent-completed",
  "secret": "请替换为足够长的随机密钥"
}
```

重载插件后，用 `owner_qq_id` 对应的 QQ 与机器人私聊并执行 `/开发通知绑定`。可用命令：

- `/开发通知帮助`
- `/开发通知状态`
- `/开发通知绑定`
- `/开发通知解绑`

插件持久化当前 `unified_msg_origin`。如果直接设置 `target_umo`，配置值优先于命令绑定值。
运行数据位于 `data/plugin_data/astrbot_plugin_agent_completion_notifier/`。

默认只监听回环地址。跨主机使用时，建议由 Caddy/Nginx 提供 HTTPS 并反向代理到
`127.0.0.1:8766`。不要在公网明文暴露端口，也不要提交密钥、QQ 标识或运行数据库。

## Webhook 协议

请求必须携带 `Authorization: Bearer <secret>`，请求体为：

```json
{
  "source": "codex",
  "event": "agent-turn-complete",
  "event_id": "codex:<thread-id>:<turn-id>",
  "session_id": "<thread-id>",
  "project": "project-name",
  "host": "<推送标题中的来源名称，可选，缺省显示为 服务器-OpenCode/Codex>",
  "message": "本轮最终回复"
}
```

`source=opencode` 时，`event` 必须是 `session.idle`。`event_id` 用于幂等去重；只有 QQ
推送成功后才记为已交付，发送失败可重试。

## Codex 客户端

复制 `examples/agent_hooks/codex_notify.py` 到客户端固定路径，并设置：

```bash
export ASTRBOT_AGENT_WEBHOOK_URL="https://你的域名/api/hooks/agent-completed"
export ASTRBOT_AGENT_WEBHOOK_TOKEN="与服务端相同的密钥"
```

在用户级 `~/.codex/config.toml` 中配置：

```toml
notify = ["python3", "/绝对路径/codex_notify.py"]
```

## OpenCode 客户端

将 `examples/agent_hooks/opencode_notification.js` 复制到项目 `.opencode/plugins/` 或全局
`~/.config/opencode/plugins/`，并为 OpenCode 进程设置相同的两个环境变量。

## 手工检查

```bash
curl -X POST "https://你的域名/api/hooks/agent-completed" \
  -H "Authorization: Bearer $ASTRBOT_AGENT_WEBHOOK_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"source":"codex","event":"agent-turn-complete","event_id":"manual:test:1","session_id":"test","project":"manual","message":"连通性测试"}'
```

注意：AstrBot 的 `qq_official` 适配器不支持这种基于会话的主动发送；请使用支持主动发送的
OneBot/aiocqhttp 等适配器。
