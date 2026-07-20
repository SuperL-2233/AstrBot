# astrbot_plugin_cugb_assistant

面向 CUGB 校园生活的 AstrBot 插件。

当前能力：

- AstrBot 可加载插件
- 提供帮助、状态、课程表、空教室、自习推荐和作业命令
- 从本地 XLSX 导入真实课程表并使用 SQLite 查询
- 空教室通过 CUGB 门户 provider 定时刷新 SQLite 缓存
- 通过 AstrBot LLM tools 提供结构化查询能力
- 接收 Codex/OpenCode 一轮结束 webhook，并通过已绑定的 AstrBot QQ 私聊主动推送

尚未实现：

- 学习通在线登录和作业抓取
- 课前提醒与作业提醒任务
- 基于真实地理距离的自习教室推荐

未实现功能会返回明确状态，不使用模拟 provider 或模拟生产数据。

数据默认存放在 `AstrBot/data/plugin_data/astrbot_plugin_cugb_assistant/`。

## Codex/OpenCode 结束通知

### 1. 服务端配置

先在 AstrBot 插件配置中设置 `user.qq_id`，并配置：

```json
{
  "agent_notification": {
    "enabled": true,
    "host": "127.0.0.1",
    "port": 8766,
    "path": "/api/hooks/agent-completed",
    "secret": "请替换为足够长的随机密钥"
  }
}
```

重载插件后，用配置的 QQ 与机器人私聊并执行 `/开发通知绑定`。插件会持久化当前
`unified_msg_origin`，用于 AstrBot 主动发送；`/开发通知解绑` 可清除绑定。若直接配置
`agent_notification.target_umo`，该配置值优先于命令绑定值。

接收器默认只监听回环地址。跨主机使用时，建议由 Caddy/Nginx 提供 HTTPS 并反向代理到
`127.0.0.1:8766`，不要在公网明文暴露端口。密钥、QQ 标识和运行数据库都属于运行时数据，
不得提交到 Git。

统一请求格式如下：

```json
{
  "source": "codex",
  "event": "agent-turn-complete",
  "event_id": "codex:<thread-id>:<turn-id>",
  "session_id": "<thread-id>",
  "project": "project-name",
  "message": "本轮最终回复"
}
```

请求必须携带 `Authorization: Bearer <secret>`。`source=opencode` 时，`event` 必须是
`session.idle`。`event_id` 用于幂等去重；只有 QQ 推送成功后才记为已交付，发送失败可重试。

### 2. Codex 客户端

复制 `examples/agent_hooks/codex_notify.py` 到客户端的固定绝对路径，设置环境变量：

```bash
export ASTRBOT_AGENT_WEBHOOK_URL="https://你的域名/api/hooks/agent-completed"
export ASTRBOT_AGENT_WEBHOOK_TOKEN="与服务端相同的密钥"
```

在用户级 `~/.codex/config.toml` 中配置外部通知程序（项目级配置不能设置 `notify`）：

```toml
notify = ["python3", "/绝对路径/codex_notify.py"]
```

Codex 会把 `agent-turn-complete` JSON 作为脚本的单个命令行参数传入；适配脚本抽取会话、
轮次和最终回复后调用统一 webhook。

### 3. OpenCode 客户端

将 `examples/agent_hooks/opencode_notification.js` 复制到项目的 `.opencode/plugins/`，或复制到
全局 `~/.config/opencode/plugins/`。为启动 OpenCode 的进程设置相同的两个环境变量。插件监听
官方 `session.idle` 事件，并通过 SDK 读取最后一条 assistant 文本后调用统一 webhook。

### 4. 手工连通性检查

```bash
curl -X POST "https://你的域名/api/hooks/agent-completed" \
  -H "Authorization: Bearer $ASTRBOT_AGENT_WEBHOOK_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"source":"codex","event":"agent-turn-complete","event_id":"manual:test:1","session_id":"test","project":"manual","message":"连通性测试"}'
```

参考：[AstrBot 主动发送消息](https://docs.astrbot.app/en/dev/star/guides/send-message.html)、
[OpenCode 插件事件](https://opencode.ai/docs/plugins/)、
[OpenCode SDK](https://opencode.ai/docs/sdk/)、
[Codex 配置](https://developers.openai.com/codex/config-advanced)。
