# CUGB 教务处通知插件

独立 AstrBot 插件，使用 vendored `SuperL-2233/CUGBJwcAPI` 只读客户端同步 CUGB 教务处公开通知。

## 行为

- 默认启用并每 10 分钟读取通知列表第一页。
- 首次成功同步只建立本地基线，不推送历史通知。
- 后续发现新通知时写入持久化待推送队列，并向绑定的 QQ 私聊发送标题、日期和原文链接。
- 发送失败或未绑定目标时保留队列，下次同步或绑定后重试。
- 只为新通知或正文尚未成功缓存的通知读取正文；正文和附件链接存入独立 SQLite。
- 同步失败不会清空旧缓存，也不会伪造结果。

## 使用

先在目标 QQ 私聊执行 `/教务通知绑定`，再使用：

- `/教务通知状态`
- `/教务通知同步`
- `/教务通知搜索 <正文关键词>`
- `/教务通知解绑`

LLM 可以主动调用：

- `refresh_jwc_notices`
- `search_jwc_notice_contents`

若设置 `owner_qq_id`，只有该 QQ 用户可以在私聊绑定或解绑目标。也可以直接配置 `target_umo`，它优先于命令绑定值。

运行数据位于 `data/plugin_data/astrbot_plugin_cugb_jwc_notice/`，不得提交 SQLite 文件或 QQ 会话标识。

## 上游来源

只读网页客户端与解析器来自 `SuperL-2233/CUGBJwcAPI` 的提交 `d9e3696bf7196715092d3911c516e5890b098eab`（v1.1.0，MIT）。详见 `providers/vendor/UPSTREAM.md` 和随附许可证。
