from __future__ import annotations

import json
from contextlib import suppress

from astrbot.api import llm_tool, logger, star
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.core.star.filter.command import GreedyStr

from .formatters.weather_formatter import (
    format_location_status,
    format_push_time_status,
    format_weather_report,
)
from .providers.baidu_weather_provider import BaiduWeatherProvider
from .repositories.settings_repo import SettingsRepository
from .services.state_service import StateService
from .services.weather_service import WeatherService
from .utils.config_loader import load_plugin_config
from .utils.paths import resolve_data_path
from .utils.permission_utils import ensure_general_access
from .utils.time_utils import today_local

PUSH_JOB_NAME = "baidu_weather_daily_push"
REFRESH_JOB_NAME = "baidu_weather_push_refresh"


@star.register(
    "astrbot_plugin_baidu_weather",
    "macroladlezilla",
    "百度天气查询与定时推送",
    "0.1.0",
)
class BaiduWeatherPlugin(star.Star):
    def __init__(self, context: star.Context, config: dict | None = None) -> None:
        super().__init__(context, config)
        self.config = load_plugin_config(config)
        repo = SettingsRepository(resolve_data_path("weather.sqlite"))
        self.state_service = StateService(self.config, repo)
        self.weather_service = WeatherService(
            self.config, BaiduWeatherProvider(self.config), self.state_service
        )
        self._push_job_id: str | None = None
        self._refresh_job_id: str | None = None

    async def initialize(self) -> None:
        logger.info("astrbot_plugin_baidu_weather initialized")
        try:
            refresh_job = await self.context.cron_manager.add_basic_job(
                name=REFRESH_JOB_NAME,
                cron_expression="5 0 * * *",
                handler=self._refresh_push_job,
                timezone=self.state_service.tz_name,
                persistent=False,
            )
            self._refresh_job_id = refresh_job.job_id
            await self._refresh_push_job()
        except Exception as exc:
            logger.warning(f"Baidu weather push jobs registration failed: {exc}")

    async def terminate(self) -> None:
        for job_id in (self._push_job_id, self._refresh_job_id):
            if job_id:
                with suppress(Exception):
                    await self.context.cron_manager.delete_job(job_id)

    async def _refresh_push_job(self) -> None:
        """(Re)register the daily push job at today's effective push time.

        The effective time can change when the user edits settings or when a
        dated override starts/ends (handled by the daily 00:05 refresh job).
        """
        hhmm = self.state_service.effective_push_time(
            today_local(self.state_service.tz_name)
        )
        hour, minute = hhmm.split(":")
        cron_expression = f"{int(minute)} {int(hour)} * * *"
        if self._push_job_id:
            job = await self.context.cron_manager.update_job(
                self._push_job_id, cron_expression=cron_expression
            )
            if job:
                return
        job = await self.context.cron_manager.add_basic_job(
            name=PUSH_JOB_NAME,
            cron_expression=cron_expression,
            handler=self._push_weather,
            timezone=self.state_service.tz_name,
            persistent=False,
        )
        self._push_job_id = job.job_id

    async def _push_weather(self) -> None:
        if not self.state_service.push_enabled():
            return
        umo = self.state_service.get_target_umo()
        if not umo:
            logger.info("Baidu weather push skipped: no target session bound")
            return
        result = await self.weather_service.query_weather("", 1)
        if not result["success"]:
            logger.warning(f"Baidu weather push failed: {result['message']}")
            return
        text = format_weather_report(result["data"], greeting="早上好！今日天气：")
        await self.context.send_message(umo, MessageChain().message(text))

    @filter.command("天气帮助")
    async def help_command(self, event: AstrMessageEvent):
        """显示天气助手命令帮助。"""
        access_error = ensure_general_access(event, self.config)
        if access_error:
            yield event.plain_result(access_error)
            return
        yield event.plain_result(
            "天气助手命令\n"
            "/天气 [地点] 查询当天天气\n"
            "/天气位置 [地点] 查看或设置常驻位置\n"
            "/天气推送时间 [HH:MM] 查看或设置常驻推送时间\n"
            "/天气推送绑定 将每日推送绑定到当前会话\n"
            "/天气推送开关 [开|关] 查看或切换每日推送\n"
            "也可以直接对我说：我现在在×× / 我未来几天都会在×× / "
            "以后每天8点推天气 / 查一下未来三天天气"
        )

    @filter.command("天气")
    async def weather_command(self, event: AstrMessageEvent, location: GreedyStr = ""):
        """查询当天天气，可指定地点。"""
        access_error = ensure_general_access(event, self.config)
        if access_error:
            yield event.plain_result(access_error)
            return
        result = await self.weather_service.query_weather(str(location).strip(), 1)
        if not result["success"]:
            yield event.plain_result(result["message"])
            return
        yield event.plain_result(format_weather_report(result["data"]))

    @filter.command("天气位置")
    async def location_command(self, event: AstrMessageEvent, location: GreedyStr = ""):
        """查看位置状态或设置常驻位置。"""
        access_error = ensure_general_access(event, self.config)
        if access_error:
            yield event.plain_result(access_error)
            return
        address = str(location).strip()
        if address:
            result = await self.weather_service.set_location(address)
            yield event.plain_result(result["message"])
            return
        yield event.plain_result(format_location_status(self.state_service.status()))

    @filter.command("天气推送时间")
    async def push_time_command(self, event: AstrMessageEvent, time_text: str = ""):
        """查看推送时间状态或设置常驻推送时间。"""
        access_error = ensure_general_access(event, self.config)
        if access_error:
            yield event.plain_result(access_error)
            return
        time_text = time_text.strip()
        if time_text:
            result = self.weather_service.set_push_time(time_text)
            if result["success"]:
                with suppress(Exception):
                    await self._refresh_push_job()
            yield event.plain_result(result["message"])
            return
        yield event.plain_result(format_push_time_status(self.state_service.status()))

    @filter.command("天气推送绑定")
    async def bind_command(self, event: AstrMessageEvent):
        """将每日天气推送绑定到当前会话。"""
        access_error = ensure_general_access(event, self.config)
        if access_error:
            yield event.plain_result(access_error)
            return
        self.state_service.set_target_umo(event.unified_msg_origin)
        yield event.plain_result("已将每日天气推送绑定到当前会话。")

    @filter.command("天气推送开关")
    async def push_toggle_command(self, event: AstrMessageEvent, action: str = ""):
        """查看或切换每日推送开关。"""
        access_error = ensure_general_access(event, self.config)
        if access_error:
            yield event.plain_result(access_error)
            return
        action = action.strip()
        if action in ("开", "开启", "on"):
            self.state_service.set_push_enabled(True)
            yield event.plain_result("已开启每日天气推送。")
        elif action in ("关", "关闭", "off"):
            self.state_service.set_push_enabled(False)
            yield event.plain_result("已关闭每日天气推送。")
        else:
            state = "开" if self.state_service.push_enabled() else "关"
            yield event.plain_result(
                f"每日天气推送当前为：{state}。用法：/天气推送开关 开|关"
            )

    @llm_tool("get_weather")
    async def tool_get_weather(
        self, _context, location: str = "", days: int = 1, hourly: bool = False
    ) -> str:
        """Get 1-7 day weather; blank location uses saved place (trip first). Use
        hourly=true for hourly forecasts and rain or peak-temperature timing.

        Args:
            location(string): Place name; blank uses the saved location.
            days(number): Forecast days, 1-7; default 1.
            hourly(boolean): Include 24-hour detail; default false.

        Returns:
            JSON with the query or operation result.
        """
        access_error = ensure_general_access(_context, self.config)
        if access_error:
            return json.dumps(
                {"success": False, "message": access_error},
                ensure_ascii=False,
                separators=(",", ":"),
            )
        result = await self.weather_service.query_weather(location, days)
        if result["success"] and not hourly:
            result = {
                **result,
                "data": {
                    key: value
                    for key, value in result["data"].items()
                    if key != "hours"
                },
            }
        return json.dumps(result, ensure_ascii=False, separators=(",", ":"))

    @llm_tool("set_user_location")
    async def tool_set_user_location(
        self, _context, location: str, start_date: str = "", end_date: str = ""
    ) -> str:
        """Set weather location: no dates for resident, both dates for a trip, then
        restore. Resolve relative dates from today.

        Args:
            location(string): Place name.
            start_date(string): Trip start, YYYY-MM-DD; optional.
            end_date(string): Trip end, YYYY-MM-DD; optional.

        Returns:
            JSON with the query or operation result.
        """
        access_error = ensure_general_access(_context, self.config)
        if access_error:
            return json.dumps(
                {"success": False, "message": access_error},
                ensure_ascii=False,
                separators=(",", ":"),
            )
        result = await self.weather_service.set_location(location, start_date, end_date)
        return json.dumps(result, ensure_ascii=False, separators=(",", ":"))

    @llm_tool("set_weather_push_time")
    async def tool_set_weather_push_time(
        self, _context, time: str, start_date: str = "", end_date: str = ""
    ) -> str:
        """Set daily weather push time: no dates for permanent, both for temporary,
        then restore. Resolve relative dates from today.

        Args:
            time(string): Daily push time, HH:MM.
            start_date(string): Start, YYYY-MM-DD; optional.
            end_date(string): End, YYYY-MM-DD; optional.

        Returns:
            JSON with the query or operation result.
        """
        access_error = ensure_general_access(_context, self.config)
        if access_error:
            return json.dumps(
                {"success": False, "message": access_error},
                ensure_ascii=False,
                separators=(",", ":"),
            )
        result = self.weather_service.set_push_time(time, start_date, end_date)
        if result["success"]:
            with suppress(Exception):
                await self._refresh_push_job()
        return json.dumps(result, ensure_ascii=False, separators=(",", ":"))
