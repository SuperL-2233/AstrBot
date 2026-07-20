# astrbot_plugin_cugb_assistant

面向 CUGB 校园生活的 AstrBot 插件。

当前能力：

- AstrBot 可加载插件
- 提供帮助、状态、课程表、空教室、自习推荐和作业命令
- 从本地 XLSX 导入真实课程表并使用 SQLite 查询
- 空教室通过 CUGB 门户 provider 定时刷新 SQLite 缓存
- 通过 AstrBot LLM tools 提供结构化查询能力

尚未实现：

- 学习通在线登录和作业抓取
- 课前提醒与作业提醒任务
- 基于真实地理距离的自习教室推荐

未实现功能会返回明确状态，不使用模拟 provider 或模拟生产数据。

数据默认存放在 `AstrBot/data/plugin_data/astrbot_plugin_cugb_assistant/`。
