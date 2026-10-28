# astrbot_plugin_agent_completion_notifier

独立的 AstrBot 开发工具结束通知插件。接收配置中允许的 AI agent 一轮结束事件，并通过已绑定的
AstrBot 会话主动推送到 QQ。附带 Codex/OpenCode 客户端示例，不依赖 CUGB 校园助手插件。

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

`allowed_sources` 仍默认为 `["codex", "opencode"]`，旧配置无需迁移。接入其他 agent 时，
将其 `source` 名称加入列表即可，无需修改服务端代码。来源名称比较不区分大小写；
空列表仍表示拒绝所有来源。现有鉴权、绑定和数据库配置继续生效。

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
  "host": "<推送标题中的来源名称，可选，缺省显示为 服务器-来源名称>",
  "message": "本轮最终回复"
}
```

Codex/OpenCode 的现有回调保持兼容：`source=codex` 时，`event` 必须是
`agent-turn-complete`；`source=opencode` 时，`event` 必须是 `session.idle`。
其他已配置来源可自行定义非空 `event` 名称（最长 64 字符），客户端仅应在一轮结束时发送。
所有来源仍须提供非空字符串 `source`（最长 32 字符）、`event_id` 和 `session_id`
（各最长 200 字符）。`event_id` 用于全局幂等去重，建议包含来源和会话标识；只有 QQ
推送成功后才记为已交付，发送失败可重试。未指定 `host` 时，Codex/OpenCode 保持原有显示名，
其他来源显示为 `服务器-<小写 source>`。

例如，在 `allowed_sources` 中加入 `custom-agent` 后，客户端可发送：

```json
{
  "source": "custom-agent",
  "event": "turn.finished",
  "event_id": "custom-agent:<session-id>:<turn-id>",
  "session_id": "<session-id>",
  "message": "本轮最终回复"
}
```

其他 agent 需要自行适配上述 HTTP 协议；插件未内置其客户端 hook。

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
