from __future__ import annotations

import json

from astrbot.api import llm_tool, logger, star
from astrbot.api.event import AstrMessageEvent, filter

from .formatters.bus_formatter import format_bus_arrivals, format_line_schedule
from .providers.chelaile_provider import ChelaileProvider
from .services.bus_service import BusService
from .utils.config_loader import load_plugin_config
from .utils.permission_utils import ensure_general_access


@star.register(
    "astrbot_plugin_chelaile_bus",
    "macroladlezilla",
    "车来了公交实时到站查询插件",
    "0.3.0",
)
class ChelaileBusPlugin(star.Star):
    def __init__(self, context: star.Context, config: dict | None = None) -> None:
        super().__init__(context, config)
        self.config = load_plugin_config(config)
        bus_cfg = self.config["bus"]
        self.provider = (
            ChelaileProvider(self.config) if bus_cfg.get("enabled", True) else None
        )
        self.bus_service = BusService(self.config, self.provider)

    async def initialize(self) -> None:
        logger.info("astrbot_plugin_chelaile_bus initialized")

    async def terminate(self) -> None:
        if self.provider is not None:
            await self.provider.close()

    @filter.command("公交帮助", alias={"bus帮助"})
    async def bus_help(self, event: AstrMessageEvent):
        """显示公交查询命令帮助。"""
        text = "\n".join(
            [
                "公交查询命令",
                "/公交 <线路号> <站点名称>",
                "例如：/公交 368 苏州桥南",
                "/公交首末班 <线路号>",
                "例如：/公交首末班 76",
                "站点名称支持模糊匹配，匹配到多个站点时会返回候选列表。",
                "也可以直接发消息问我，例如“368 路到苏州桥南还有几站”。",
            ]
        )
        yield event.plain_result(text)

    @filter.command("公交", alias={"bus"})
    async def bus_query(
        self, event: AstrMessageEvent, line: str = "", station: str = ""
    ):
        """查询公交线路在指定站点的实时到站信息。"""
        access_error = ensure_general_access(event, self.config)
        if access_error:
            yield event.plain_result(access_error)
            return
        if not line.strip() or not station.strip():
            yield event.plain_result(
                "用法：/公交 <线路号> <站点名称>，例如 /公交 368 苏州桥南"
            )
            return
        result = await self.bus_service.query_arrivals(line, station)
        yield event.plain_result(format_bus_arrivals(result))

    @filter.command("公交首末班", alias={"首末班"})
    async def bus_schedule(self, event: AstrMessageEvent, line: str = ""):
        """查询公交线路各方向的首末班发车时间。"""
        access_error = ensure_general_access(event, self.config)
        if access_error:
            yield event.plain_result(access_error)
            return
        if not line.strip():
            yield event.plain_result("用法：/公交首末班 <线路号>，例如 /公交首末班 76")
            return
        result = await self.bus_service.get_line_schedule(line)
        yield event.plain_result(format_line_schedule(result))

    @llm_tool("query_bus_arrival")
    async def tool_query_bus_arrival(
        self, _context, line: str = "", station: str = "", city_id: str = ""
    ) -> str:
        """查询指定公交线路在某一站点的实时到站信息，包括距站距离、剩余站数、预计行驶时长和预计到站时间。站点名称支持模糊匹配：匹配到唯一候选时自动使用，匹配到多个候选时返回候选列表。

        Args:
            line(string): 公交线路号，必须以字符串形式传入，例如 "368"
            station(string): 公交站点名称，例如 "苏州桥南"，不要求完全精确
            city_id(string): 可选，车来了城市 ID，留空时使用插件默认配置
        """
        if not self.config["llm"].get("enable_tools", True):
            return json.dumps(
                {"success": False, "message": "公交查询 LLM 工具未启用。"},
                ensure_ascii=False,
            )
        return json.dumps(
            await self.bus_service.query_arrivals(line, station, city_id),
            ensure_ascii=False,
        )

    @llm_tool("search_bus_station")
    async def tool_search_bus_station(
        self, _context, keyword: str = "", city_id: str = ""
    ) -> str:
        """按模糊关键词检索公交站和附近地点，用于在查询实时公交前确认准确的站点名称。

        Args:
            keyword(string): 模糊地名或站点关键词，必须以字符串形式传入，例如 "翠微路口"
            city_id(string): 可选，车来了城市 ID，留空时使用插件默认配置
        """
        if not self.config["llm"].get("enable_tools", True):
            return json.dumps(
                {"success": False, "message": "公交查询 LLM 工具未启用。"},
                ensure_ascii=False,
            )
        return json.dumps(
            await self.bus_service.search_stations(keyword, city_id),
            ensure_ascii=False,
        )

    @llm_tool("query_bus_line_schedule")
    async def tool_query_bus_line_schedule(
        self, _context, line: str = "", city_id: str = ""
    ) -> str:
        """查询公交线路各方向的首末班发车时间、起终点站、票价和站点数量。

        Args:
            line(string): 公交线路号，必须以字符串形式传入，例如 "76"
            city_id(string): 可选，车来了城市 ID，留空时使用插件默认配置
        """
        if not self.config["llm"].get("enable_tools", True):
            return json.dumps(
                {"success": False, "message": "公交查询 LLM 工具未启用。"},
                ensure_ascii=False,
            )
        return json.dumps(
            await self.bus_service.get_line_schedule(line, city_id),
            ensure_ascii=False,
        )
