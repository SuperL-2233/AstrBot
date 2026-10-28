# astrbot_plugin_agent_completion_notifier

独立的 AstrBot「通知接收与推送」插件。接收自定义标题、正文的通用通知，以及配置中允许的
AI agent 一轮结束事件，通过同一已绑定的 AstrBot 会话主动推送到 QQ。
插件 ID、旧 agent 接口、原有配置和运行数据目录保持兼容；附带 Codex/OpenCode 客户端示例。

## 服务端配置

```json
{
  "owner_qq_id": "你的 QQ 号",
  "enabled": true,
  "host": "127.0.0.1",
  "port": 8766,
  "path": "/api/hooks/agent-completed",
  "notify_path": "/api/hooks/notify",
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

新增的 `notify_path` 未配置时使用 `/api/hooks/notify`；它须与原有 agent `path` 不同，
否则接收器会明确报错并拒绝启动。两个入口共用 `enabled`、监听地址/端口、`secret`、
QQ 目标和消息长度限制。反向代理需要同时转发这两个路径。

`allowed_sources` 仍默认为 `["codex", "opencode"]`，只限制 agent 接口。接入其他 agent 时，
将其 `source` 名称加入列表即可，无需修改服务端代码。来源名称比较不区分大小写；
空列表仍表示拒绝所有 agent 来源，通用通知接口仍可接收已鉴权的请求。

默认只监听回环地址。跨主机使用时，建议由 Caddy/Nginx 提供 HTTPS 并反向代理到
`127.0.0.1:8766`。不要在公网明文暴露端口，也不要提交密钥、QQ 标识或运行数据库。

## 通用通知接口

向 `notify_path`（默认 `/api/hooks/notify`）发送 `POST` 请求，携带
`Authorization: Bearer <secret>` 和 JSON 请求体：

```json
{
  "title": "备份完成",
  "message": "文件已上传。\n报告：https://example.com/report",
  "source": "backup",
  "event_id": "backup:<job-id>"
}
```

- `message`：必填非空字符串，最长 12000 字符。
- `title`：可选字符串，最长 200 字符；有标题时显示为「标题 + 空行 + 正文」。
- `source`：可选来源名称，最长 32 字符，仅记录为来源，不添加到通知正文；省略时记为 `generic`。
- `event_id`：可选去重标识，最长 200 字符；与 agent 通知共用去重空间，应使用包含来源的唯一标识。

只发送 `{"message":"你想发送的内容"}` 即可推送纯正文。标题和正文只去除首尾空白，
保留内部换行、链接和 Markdown 字符，按纯文本发送；不添加「会话结束」、来源或会话编号。
最终消息仍受 `max_message_chars` 限制（默认 1500 字符，超出时截断并标注）。
不提供 `event_id`（或值为空/null）时，每次请求均发送，不生成去重记录。
提供非空 `event_id` 后，重复请求返回 `duplicate: true`，去重记录在重启后保留；
发送失败或任务取消会释放该 ID，调用方可使用同一 ID 重试。插件不在后台自动重试发送。

```bash
curl -X POST "https://你的域名/api/hooks/notify" \
  -H "Authorization: Bearer $ASTRBOT_AGENT_WEBHOOK_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"title":"自定义标题","message":"第一行正文\n第二行正文"}'
```

成功响应为 `{"success":true,"duplicate":false,"message":"通知已推送。"}`。
两个接口均返回：鉴权失败 `401`、JSON/字段校验失败 `400`、请求体过大 `413`、
未绑定 QQ 目标 `503`、发送失败 `502`。HTTP 请求体上限由 `max_body_bytes` 设置，默认 16384 字节。

## Agent 完成事件接口（兼容旧协议）

向原有 `path`（默认 `/api/hooks/agent-completed`）发送 `POST` 请求，携带相同的
`Authorization: Bearer <secret>`，请求体为：

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

推送依赖目标平台适配器的主动发送能力。当前工作树的 `qq_official` 支持 C2C 私聊主动发送；
OneBot/aiocqhttp 等适配器也可使用。接口返回成功表示 AstrBot 已找到并调用目标适配器，
不表示接收人已读。
