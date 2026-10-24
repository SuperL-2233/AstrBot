# astrbot_plugin_cugb_assistant

面向 CUGB 校园生活的 AstrBot 插件。

当前能力：

- AstrBot 可加载插件
- 提供帮助、空教室和自习推荐命令
- 空教室通过 CUGB 门户 provider 定时刷新 SQLite 缓存
- 通过 AstrBot LLM tools 提供结构化查询能力

LLM 空教室查询保留全部匹配教室，将各行相同的起止时间、来源和更新时间移到 `query`；不同的更新时间继续随对应教室返回。`recommend_study_room` 默认返回排名前 5 个结果，并报告 `total_count`、`returned_count` 和 `has_more`；可指定 `limit`，或用 `limit=0` 返回全部。推荐失败会保留真实来源错误。未实现的 `current_location` 参数已移除，下一节课地点仍可通过 `next_course_location` 影响排序。

尚未实现：

- 基于真实地理距离的自习教室推荐

未实现功能会返回明确状态，不使用模拟 provider 或模拟生产数据。

状态查询由 `astrbot_plugin_capability_registry` 插件负责；课程表查询和课前提醒由 `astrbot_plugin_cugb_jwgl` 插件负责；学习通查询和截止提醒由独立的 `astrbot_plugin_chaoxing_deadline` 插件负责。

数据默认存放在 `AstrBot/data/plugin_data/astrbot_plugin_cugb_assistant/`。
