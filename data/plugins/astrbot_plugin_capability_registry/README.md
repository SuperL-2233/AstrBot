# astrbot_plugin_capability_registry

独立的 AstrBot 插件能力与状态查询入口。

它使用 AstrBot `Context.get_all_stars()` 枚举所有已注册插件，并约定业务插件可选实现：

- `get_public_capabilities()`：返回简介、功能、命令和 LLM 工具。
- `get_public_status()`：返回不含凭据和个人数据的运行状态。

没有实现协议的插件仍会出现在结果中，但只返回 AstrBot 元数据，并明确标记未提供详细状态。这不是 AstrBot 官方强制协议，不会修改框架或要求第三方插件适配。

接口：

- `/插件状态 [插件名称关键词]`
- `get_plugin_status(plugin_name="")`
