# CUGB 新闻公告检索插件

独立 AstrBot 插件，使用 vendored `SuperL-2233/CUGBAPI` 只读客户端，统一同步 CUGB 六个公开栏目的新闻公告到本地数据库，并支持推送与检索。

## 来源

| 来源键 | 栏目 | 默认地址 |
| --- | --- | --- |
| `jwc_student` | 教务处·学生专区 | `https://jwc.cugb.edu.cn/xszq/` |
| `jwc_teaching` | 教务处·教学动态 | `https://jwc.cugb.edu.cn/jxdt/` |
| `ai_news` | 人工智能学院·新闻动态 | `https://sai.cugb.edu.cn/xyxw/` |
| `ai_notice` | 人工智能学院·公告 | `https://sai.cugb.edu.cn/xygg/` |
| `xgb_news` | 学工处·新闻动态 | `https://bm.cugb.edu.cn/xgb/jjxg/` |
| `xgb_notice` | 学工处·通知公告 | `https://bm.cugb.edu.cn/xgb/tzgg/` |

所有来源写入同一个 SQLite 数据库，按 `source + notice_id` 区分，查询可按来源过滤。

## 行为

- 默认启用并每 10 分钟同步一次全部来源；首次持续翻页直到覆盖 `2025-09-01`（包含当天）。
- 完成历史覆盖后，常规定时同步在遇到已缓存条目时停止翻页，避免每 10 分钟重复读取全部历史页面；每个来源独立记录覆盖进度。
- 教务处“学生专区”静态列表当前只保留 10 页；若列表在目标日期前提前结束，插件会使用该官网页面自身调用的公开 CMS 搜索接口补齐历史条目。其余来源列表页可完整翻页时不使用搜索接口。
- 某个来源同步失败不影响其他来源，也不会清空该来源旧缓存。
- 首次成功同步只建立本地基线，不推送历史条目；用户明确指定更早日期所产生的历史回填也只入库。
- 后续发现新条目时写入持久化待推送队列，并向绑定的 QQ 私聊发送来源、标题、日期和原文链接。
- 发送失败或未绑定目标时保留队列，下次同步或绑定后重试；每 10 条一批发送并逐批确认。
- 只为新条目或正文尚未成功缓存的条目读取正文；缺失正文单独限量重试。
- 刷新结果分别返回“本次扫描条数”和“数据库总数”，两者不混用。

## 使用

先在目标 QQ 私聊执行 `/新闻公告绑定`，再使用：

- `/新闻公告状态`
- `/新闻公告同步 [来源] [最早日期 YYYY-MM-DD]`
- `/新闻公告搜索 <正文关键词>`
- `/新闻公告列表 [来源] [开始日期] [结束日期]`
- `/新闻公告统计 [来源] [开始日期] [结束日期]`
- `/新闻公告解绑`

命令中的“来源”可填写来源键或别名：教务、教学动态、AI新闻、AI公告、学工新闻、学工公告；留空表示全部来源。旧版 `/教务通知…` 命令已随插件改名移除。

LLM 可以主动调用：

- `refresh_cugb_notices`
- `search_cugb_notice_contents`
- `list_cugb_notices`
- `get_cugb_notice_stats`

四个工具都接受可选 `source` 参数（六个来源键之一，留空为全部）。列表和正文搜索都是分页接口，返回独立的 `total_count`、`returned_count`、`offset` 和 `has_more`。询问总数时应调用 `get_cugb_notice_stats`，不能用某一页的返回数推断。正文搜索只返回有界摘要与链接，不把整篇正文塞入模型上下文。

若设置 `owner_qq_id`，只有该 QQ 用户可以在私聊绑定或解绑目标。也可以直接配置 `target_umo`，它优先于命令绑定值。

`sync_since_date` 控制后台默认追溯日期，默认是 `2025-09-01`。命令或 `refresh_cugb_notices` 工具可以为单次同步传入 `earliest_date`，例如 `2024-09-01`；这不会修改后台默认值。`max_list_pages` 默认 200，`max_search_pages_per_year` 默认 100；两者只是防止异常页面导致无界翻页的安全上限。六个来源地址均可通过配置覆盖，必须是 HTTPS。

运行数据位于 `data/plugin_data/astrbot_plugin_cugb_jwc_notice/`，不得提交 SQLite 文件或 QQ 会话标识。0.4.0 之前的旧版数据库会在首次启动时自动迁移到统一多来源结构，原有教务处条目归入 `jwc_student`。

## 上游来源

只读网页客户端与解析器来自 `SuperL-2233/CUGBAPI` 的提交 `6ae832cb6fb18f6ad85c2407043cad054cae2b6d`（v2.1.0，MIT）。详见 `providers/vendor/UPSTREAM.md` 和随附许可证。
