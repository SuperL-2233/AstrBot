from __future__ import annotations

import asyncio
from contextlib import suppress

from astrbot.api import llm_tool, logger, star
from astrbot.api.event import AstrMessageEvent, filter

from .formatters.classroom_formatter import (
    format_classroom_list,
    format_classroom_tool_result,
    format_recommendations,
)
from .providers.classroom_cache_provider import ClassroomCacheProvider
from .providers.classroom_mportal_provider import ClassroomMportalProvider
from .repositories.classroom_repo import ClassroomRepository
from .services.classroom_service import ClassroomService
from .services.distance_service import DistanceService
from .utils.config_loader import load_plugin_config
from .utils.paths import resolve_data_path
from .utils.permission_utils import ensure_general_access
from .utils.time_utils import combine_date_time, today_local


@star.register(
    "astrbot_plugin_cugb_assistant",
    "macroladlezilla",
    "CUGB 校园助手插件",
    "0.2.0",
)
class CugbAssistantPlugin(star.Star):
    def __init__(self, context: star.Context, config: dict | None = None) -> None:
        super().__init__(context, config)
        self.config = load_plugin_config(config)
        self._classroom_refresh_task: asyncio.Task | None = None

        classroom_repo = ClassroomRepository(
            resolve_data_path(self.config["classroom"]["database"])
        )

        classroom_provider_name = self.config["classroom"]["provider"]
        if classroom_provider_name == "cache":
            classroom_provider = ClassroomCacheProvider(classroom_repo)
        elif classroom_provider_name == "mportal":
            classroom_provider = ClassroomMportalProvider(classroom_repo, self.config)
        else:
            classroom_provider = None
        self.classroom_service = ClassroomService(self.config, classroom_provider)

        self.distance_service = DistanceService()

    async def initialize(self) -> None:
        logger.info("astrbot_plugin_cugb_assistant initialized")
        classroom_cfg = self.config["classroom"]
        if (
            classroom_cfg.get("enabled", True)
            and classroom_cfg.get("provider") == "mportal"
        ):
            self._classroom_refresh_task = asyncio.create_task(
                self._run_classroom_refresh_loop()
            )

    async def terminate(self) -> None:
        if self._classroom_refresh_task:
            self._classroom_refresh_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._classroom_refresh_task

    async def _run_classroom_refresh_loop(self) -> None:
        interval_seconds = (
            max(1, int(self.config["classroom"].get("update_interval_minutes", 120)))
            * 60
        )
        while True:
            try:
                result = await self.classroom_service.refresh()
                if result["success"]:
                    logger.info(f"空教室缓存刷新完成: {result['data']}")
                else:
                    logger.warning(f"空教室缓存刷新失败: {result['message']}")
            except Exception as exc:
                logger.warning(f"空教室缓存刷新任务异常: {exc}")
            await asyncio.sleep(interval_seconds)

    def _general_access_error(self, event: AstrMessageEvent) -> str | None:
        return ensure_general_access(event, self.config)

    async def get_public_status(self) -> dict:
        """Return CUGB classroom status for the capability registry."""
        return {"classroom": await self.classroom_service.get_status()}

    def get_public_capabilities(self) -> dict:
        """Describe public CUGB features using the project plugin contract."""
        return {
            "summary": "提供 CUGB 空教室查询和自习推荐能力。",
            "features": [
                "空教室查询",
                "自习教室推荐",
            ],
            "commands": [
                "/空教室",
                "/自习推荐",
            ],
            "llm_tools": [
                "get_empty_classrooms_now",
                "get_empty_classrooms_by_time",
                "recommend_study_room",
            ],
        }

    @filter.command("校园助手帮助", alias={"cugb帮助", "校园帮助"})
    async def help_command(self, event: AstrMessageEvent):
        """显示校园助手命令帮助。"""
        access_error = self._general_access_error(event)
        if access_error:
            yield event.plain_result(access_error)
            return
        text = "\n".join(
            [
                "校园助手命令",
                "/空教室 [教学楼] [开始时间] [结束时间]",
                "/空教室刷新",
                "/自习推荐 [开始时间] [结束时间]",
            ]
        )
        yield event.plain_result(text)

    @filter.command("空教室")
    async def empty_classroom(
        self,
        event: AstrMessageEvent,
        building: str = "",
        start_time: str = "",
        end_time: str = "",
    ):
        """查询空教室。"""
        access_error = self._general_access_error(event)
        if access_error:
            yield event.plain_result(access_error)
            return
        if start_time and end_time:
            try:
                start = combine_date_time(today_local(), start_time).strftime(
                    "%Y-%m-%d %H:%M"
                )
                end = combine_date_time(today_local(), end_time).strftime(
                    "%Y-%m-%d %H:%M"
                )
            except ValueError:
                yield event.plain_result(
                    "时间格式无法识别，请使用 HH:MM，例如 /空教室 教三 14:00 16:00"
                )
                return
            result = await self.classroom_service.get_empty_classrooms_by_time(
                building=building,
                start_time=start,
                end_time=end,
            )
        else:
            result = await self.classroom_service.get_empty_classrooms_now(
                building=building
            )
        yield event.plain_result(format_classroom_list(result, "空教室查询结果"))

    @filter.command("空教室刷新")
    async def refresh_classroom(self, event: AstrMessageEvent):
        """手动刷新空教室缓存。"""
        access_error = self._general_access_error(event)
        if access_error:
            yield event.plain_result(access_error)
            return
        result = await self.classroom_service.refresh()
        yield event.plain_result(result["message"])

    @filter.command("自习推荐")
    async def study_recommendation(
        self, event: AstrMessageEvent, start_time: str = "", end_time: str = ""
    ):
        """根据当前时间或指定时间段推荐自习教室。"""
        access_error = self._general_access_error(event)
        if access_error:
            yield event.plain_result(access_error)
            return
        if start_time and end_time:
            try:
                start = combine_date_time(today_local(), start_time).strftime(
                    "%Y-%m-%d %H:%M"
                )
                end = combine_date_time(today_local(), end_time).strftime(
                    "%Y-%m-%d %H:%M"
                )
            except ValueError:
                yield event.plain_result(
                    "时间格式无法识别，请使用 HH:MM，例如 /自习推荐 13:00 15:30"
                )
                return
            classroom_result = (
                await self.classroom_service.get_empty_classrooms_by_time(
                    start_time=start, end_time=end
                )
            )
        else:
            classroom_result = await self.classroom_service.get_empty_classrooms_now()
        if not classroom_result["success"]:
            yield event.plain_result(classroom_result["message"])
            return
        ranked = self.distance_service.recommend(classroom_result["data"])
        yield event.plain_result(format_recommendations(ranked))

    @llm_tool("get_empty_classrooms_now")
    async def tool_get_empty_classrooms_now(self, _context, building: str = "") -> str:
        """查询当前可用空教室。

        Args:
            building(string): 可选，教学楼名称
        """
        return format_classroom_tool_result(
            await self.classroom_service.get_empty_classrooms_now(building)
        )

    @llm_tool("get_empty_classrooms_by_time")
    async def tool_get_empty_classrooms_by_time(
        self, _context, building: str = "", start_time: str = "", end_time: str = ""
    ) -> str:
        """查询指定时间段空教室。

        Args:
            building(string): 可选，教学楼名称
            start_time(string): 开始时间，格式 HH:MM 或 YYYY-MM-DD HH:MM
            end_time(string): 结束时间，格式 HH:MM 或 YYYY-MM-DD HH:MM
        """
        if start_time and len(start_time) == 5:
            start_time = combine_date_time(today_local(), start_time).strftime(
                "%Y-%m-%d %H:%M"
            )
        if end_time and len(end_time) == 5:
            end_time = combine_date_time(today_local(), end_time).strftime(
                "%Y-%m-%d %H:%M"
            )
        return format_classroom_tool_result(
            await self.classroom_service.get_empty_classrooms_by_time(
                building, start_time, end_time
            )
        )

    @llm_tool("recommend_study_room")
    async def tool_recommend_study_room(
        self,
        _context,
        start_time: str = "",
        end_time: str = "",
        next_course_location: str = "",
        limit: int = 5,
    ) -> str:
        """Recommend study rooms in priority order.

        Args:
            start_time(string): HH:MM or YYYY-MM-DD HH:MM; blank queries now.
            end_time(string): Same format; supply both times.
            next_course_location(string): Optional next class room.
            limit(number): Max rooms, default 5; zero returns all.

        Returns:
            JSON with ranked rooms, shared query metadata and any source error.
        """
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
            return format_classroom_tool_result(
                {
                    "success": False,
                    "message": "limit 必须为非负整数；0 表示返回全部。",
                    "error_code": "INVALID_LIMIT",
                }
            )
        if start_time and len(start_time) == 5:
            start_time = combine_date_time(today_local(), start_time).strftime(
                "%Y-%m-%d %H:%M"
            )
        if end_time and len(end_time) == 5:
            end_time = combine_date_time(today_local(), end_time).strftime(
                "%Y-%m-%d %H:%M"
            )
        result = (
            await self.classroom_service.get_empty_classrooms_by_time(
                start_time=start_time, end_time=end_time
            )
            if start_time and end_time
            else await self.classroom_service.get_empty_classrooms_now()
        )
        if not result["success"]:
            return format_classroom_tool_result(result)
        data = self.distance_service.recommend(
            result["data"], next_course_location=next_course_location
        )
        return format_classroom_tool_result(
            {**result, "data": data, "message": "已返回推荐结果。"}, limit=limit
        )
