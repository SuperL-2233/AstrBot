from __future__ import annotations

import json

from astrbot.api import llm_tool, star
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.core.star.filter.command import GreedyStr

from .services.registry_service import PluginRegistryService, format_registry


@star.register(
    "astrbot_plugin_capability_registry",
    "macroladlezilla",
    "插件能力注册表",
    "0.1.0",
)
class CapabilityRegistryPlugin(star.Star):
    def __init__(self, context: star.Context, config: dict | None = None) -> None:
        super().__init__(context, config)
        self.registry_service = PluginRegistryService(context)

    def get_public_capabilities(self) -> dict:
        return {
            "summary": "统一枚举 AstrBot 插件公开的能力与运行状态。",
            "features": ["插件发现", "公开能力查询", "公开运行状态查询"],
            "commands": ["/插件状态"],
            "llm_tools": ["get_plugin_status"],
        }

    def get_public_status(self) -> dict:
        return {"ready": True, "contract_version": 1}

    @filter.command("插件状态")
    async def plugin_status_command(
        self, event: AstrMessageEvent, plugin_name: GreedyStr = ""
    ):
        result = await self.registry_service.query(plugin_name.strip())
        yield event.plain_result(format_registry(result))

    @llm_tool("get_plugin_status")
    async def tool_get_plugin_status(self, _context, plugin_name: str = "") -> str:
        """查询所有已注册插件，或按名称查询插件的能力和运行状态。

        每个插件都会返回启用状态和元数据；实现公开能力/状态协议的插件还会返回
        命令、LLM 工具、功能清单和详细运行状态。

        Args:
            plugin_name(string): 可选，插件名称关键词；留空查询全部插件
        """
        result = await self.registry_service.query(plugin_name)
        return json.dumps(result, ensure_ascii=False)
