from __future__ import annotations

import asyncio
import json
from contextlib import suppress

from astrbot.api import llm_tool, logger, star
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.core.star.filter.command import GreedyStr

from .formatters.classroom_formatter import format_classroom_list, format_recommendations
from .formatters.course_formatter import format_course_list, format_single_course
from .formatters.homework_formatter import format_homework_list
from .formatters.status_formatter import format_status
from .importers.import_timetable import TimetableImporter
from .providers.agent_webhook_provider import AgentWebhookProvider
from .providers.chaoxing_cache_provider import ChaoxingCacheProvider
from .providers.classroom_cache_provider import ClassroomCacheProvider
from .providers.classroom_mportal_provider import ClassroomMportalProvider
from .repositories.classroom_repo import ClassroomRepository
from .repositories.course_repo import CourseRepository
from .repositories.homework_repo import HomeworkRepository
from .repositories.notification_repo import NotificationRepository
from .services.agent_notification_service import AgentNotificationService
from .services.chaoxing_service import ChaoxingService
from .services.classroom_service import ClassroomService
from .services.course_service import CourseService
from .services.distance_service import DistanceService
from .services.reminder_service import ReminderService
from .utils.config_loader import load_plugin_config
from .utils.paths import resolve_data_path
from .utils.permission_utils import ensure_general_access, ensure_personal_schedule_access
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

        course_repo = CourseRepository(resolve_data_path(self.config["course"]["database"]))
        classroom_repo = ClassroomRepository(resolve_data_path(self.config["classroom"]["database"]))
        homework_repo = HomeworkRepository(resolve_data_path(self.config["chaoxing"]["database"]))
        notification_repo = NotificationRepository(
            resolve_data_path(self.config["agent_notification"]["database"])
        )

        importer = TimetableImporter(resolve_data_path(self.config["course"]["timetable_file"]))
        self.course_service = CourseService(course_repo, importer)

        classroom_provider_name = self.config["classroom"]["provider"]
        if classroom_provider_name == "cache":
            classroom_provider = ClassroomCacheProvider(classroom_repo)
        elif classroom_provider_name == "mportal":
            classroom_provider = ClassroomMportalProvider(classroom_repo, self.config)
        else:
            classroom_provider = None
        self.classroom_service = ClassroomService(self.config, classroom_provider)

        self.distance_service = DistanceService()
        self.reminder_service = ReminderService(self.config)
        chaoxing_provider = None
        if self.config["chaoxing"].get("provider") == "cache":
            chaoxing_provider = ChaoxingCacheProvider(homework_repo)
        self.chaoxing_service = ChaoxingService(self.config, chaoxing_provider)
        self.agent_notification_service = AgentNotificationService(self.config, notification_repo)
        self.agent_webhook_provider = AgentWebhookProvider(
            self.config,
            self._deliver_agent_notification,
        )

    async def initialize(self) -> None:
        logger.info("astrbot_plugin_cugb_assistant initialized")
        classroom_cfg = self.config["classroom"]
        if classroom_cfg.get("enabled", True) and classroom_cfg.get("provider") == "mportal":
            self._classroom_refresh_task = asyncio.create_task(self._run_classroom_refresh_loop())
        try:
            await self.agent_webhook_provider.start()
            if self.agent_webhook_provider.started:
                cfg = self.config["agent_notification"]
                logger.info(f"开发工具通知接收器已监听 {cfg['host']}:{cfg['port']}{cfg['path']}")
            else:
                logger.info(f"开发工具通知接收器未启动: {self.agent_webhook_provider.disabled_reason}")
        except Exception as exc:
            logger.error(f"开发工具通知接收器启动失败: {exc}")

    async def terminate(self) -> None:
        await self.agent_webhook_provider.stop()
        if self._classroom_refresh_task:
            self._classroom_refresh_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._classroom_refresh_task

    async def _run_classroom_refresh_loop(self) -> None:
        interval_seconds = max(1, int(self.config["classroom"].get("update_interval_minutes", 120))) * 60
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

    def _schedule_access_error(self, event: AstrMessageEvent) -> str | None:
        return ensure_personal_schedule_access(event, self.config)

    def _notification_binding_access_error(self, event: AstrMessageEvent) -> str | None:
        owner_id = str(self.config["user"].get("qq_id", "")).strip()
        if not owner_id:
            return "请先在插件配置中设置 user.qq_id，才能绑定开发通知目标。"
        if not event.is_private_chat() or event.get_sender_id() != owner_id:
            return "仅允许配置的 QQ 所有者在私聊中绑定开发通知目标。"
        return None

    async def _deliver_agent_notification(self, payload: object) -> dict:
        async def send(target_umo: str, text: str) -> bool:
            return await self.context.send_message(target_umo, MessageChain().message(text))

        return await self.agent_notification_service.deliver(payload, send)

    async def _build_status(self) -> dict:
        return {
            "course": self.course_service.get_status()["data"],
            "classroom": await self.classroom_service.get_status(),
            "chaoxing": await self.chaoxing_service.get_status(),
            "reminder": self.reminder_service.get_status(),
            "agent_notification": {
                **self.agent_notification_service.get_status(),
                "listener_started": self.agent_webhook_provider.started,
                "disabled_reason": self.agent_webhook_provider.disabled_reason,
            },
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
                "/校园助手状态",
                "/课程表刷新",
                "/今天课表",
                "/明天课表",
                "/下节课",
                "/当前课程",
                "/课程 周数 <课程名>",
                "/课程 搜索 <关键词>",
                "/空教室 [教学楼] [开始时间] [结束时间]",
                "/空教室刷新",
                "/自习推荐 [开始时间] [结束时间]",
                "/作业 [最近|课程名]",
                "/学习通状态",
                "/学习通刷新",
                "/开发通知绑定",
                "/开发通知解绑",
            ]
        )
        yield event.plain_result(text)

    @filter.command("校园助手状态")
    async def status_command(self, event: AstrMessageEvent):
        """显示插件和数据状态。"""
        access_error = self._general_access_error(event)
        if access_error:
            yield event.plain_result(access_error)
            return
        yield event.plain_result(format_status(await self._build_status()))

    @filter.command("开发通知绑定")
    async def bind_agent_notification(self, event: AstrMessageEvent):
        """将开发工具结束通知绑定到当前 QQ 私聊会话。"""
        access_error = self._notification_binding_access_error(event)
        if access_error:
            yield event.plain_result(access_error)
            return
        self.agent_notification_service.bind_target(event.unified_msg_origin)
        yield event.plain_result("已将 Codex/OpenCode 对话结束通知绑定到当前私聊。")

    @filter.command("开发通知解绑")
    async def unbind_agent_notification(self, event: AstrMessageEvent):
        """清除持久化的开发工具通知目标。"""
        access_error = self._notification_binding_access_error(event)
        if access_error:
            yield event.plain_result(access_error)
            return
        self.agent_notification_service.clear_target()
        yield event.plain_result("已解除开发工具通知绑定。")

    @filter.command("课程表刷新")
    async def refresh_timetable(self, event: AstrMessageEvent):
        """重新导入本地课程表。"""
        access_error = self._schedule_access_error(event)
        if access_error:
            yield event.plain_result(access_error)
            return
        result = self.course_service.refresh_timetable()
        yield event.plain_result(result["message"])

    @filter.command("今天课表")
    async def today_courses(self, event: AstrMessageEvent):
        """查询今天所有课程。"""
        access_error = self._schedule_access_error(event)
        if access_error:
            yield event.plain_result(access_error)
            return
        yield event.plain_result(format_course_list(self.course_service.get_today_courses(), "今天课表"))

    @filter.command("明天课表")
    async def tomorrow_courses(self, event: AstrMessageEvent):
        """查询明天所有课程。"""
        access_error = self._schedule_access_error(event)
        if access_error:
            yield event.plain_result(access_error)
            return
        yield event.plain_result(format_course_list(self.course_service.get_tomorrow_courses(), "明天课表"))

    @filter.command("下节课")
    async def next_course(self, event: AstrMessageEvent):
        """查询下一节课。"""
        access_error = self._schedule_access_error(event)
        if access_error:
            yield event.plain_result(access_error)
            return
        yield event.plain_result(format_single_course(self.course_service.get_next_course(), "下一节课"))

    @filter.command("当前课程")
    async def current_course(self, event: AstrMessageEvent):
        """查询当前是否正在上课。"""
        access_error = self._schedule_access_error(event)
        if access_error:
            yield event.plain_result(access_error)
            return
        yield event.plain_result(format_single_course(self.course_service.get_current_course(), "当前课程"))

    @filter.command("课程")
    async def course_command(self, event: AstrMessageEvent, action: str, keyword: GreedyStr = ""):
        """课程辅助命令。"""
        access_error = self._schedule_access_error(event)
        if access_error:
            yield event.plain_result(access_error)
            return
        action = action.strip()
        keyword = keyword.strip()
        if action == "周数":
            result = self.course_service.get_course_weeks(keyword)
            if not result["success"]:
                yield event.plain_result(result["message"])
                return
            lines = ["课程周次"]
            for row in result["data"]:
                lines.append(f"{row['course_name']} | 周次: {row.get('weeks') or '未填写'} | 地点: {row['location']}")
            yield event.plain_result("\n".join(lines))
            return
        if action == "搜索":
            result = self.course_service.search_courses(keyword)
            yield event.plain_result(format_course_list(result, "课程搜索结果"))
            return
        yield event.plain_result("用法：/课程 周数 <课程名> 或 /课程 搜索 <关键词>")

    @filter.command("空教室")
    async def empty_classroom(self, event: AstrMessageEvent, building: str = "", start_time: str = "", end_time: str = ""):
        """查询空教室。"""
        access_error = self._general_access_error(event)
        if access_error:
            yield event.plain_result(access_error)
            return
        if start_time and end_time:
            try:
                start = combine_date_time(today_local(), start_time).strftime("%Y-%m-%d %H:%M")
                end = combine_date_time(today_local(), end_time).strftime("%Y-%m-%d %H:%M")
            except ValueError:
                yield event.plain_result("时间格式无法识别，请使用 HH:MM，例如 /空教室 教三 14:00 16:00")
                return
            result = await self.classroom_service.get_empty_classrooms_by_time(
                building=building,
                start_time=start,
                end_time=end,
            )
        else:
            result = await self.classroom_service.get_empty_classrooms_now(building=building)
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
    async def study_recommendation(self, event: AstrMessageEvent, start_time: str = "", end_time: str = ""):
        """根据当前时间和下一节课推荐自习教室。"""
        access_error = self._general_access_error(event)
        if access_error:
            yield event.plain_result(access_error)
            return
        if start_time and end_time:
            try:
                start = combine_date_time(today_local(), start_time).strftime("%Y-%m-%d %H:%M")
                end = combine_date_time(today_local(), end_time).strftime("%Y-%m-%d %H:%M")
            except ValueError:
                yield event.plain_result("时间格式无法识别，请使用 HH:MM，例如 /自习推荐 13:00 15:30")
                return
            classroom_result = await self.classroom_service.get_empty_classrooms_by_time(start_time=start, end_time=end)
        else:
            classroom_result = await self.classroom_service.get_empty_classrooms_now()
        if not classroom_result["success"]:
            yield event.plain_result(classroom_result["message"])
            return
        next_course = self.course_service.get_next_course()
        next_location = next_course["data"]["location"] if next_course["success"] else ""
        ranked = self.distance_service.recommend(classroom_result["data"], next_course_location=next_location)
        yield event.plain_result(format_recommendations(ranked))

    @filter.command("作业")
    async def homework_command(self, event: AstrMessageEvent, keyword: GreedyStr = ""):
        """查询当前未完成作业。"""
        access_error = self._general_access_error(event)
        if access_error:
            yield event.plain_result(access_error)
            return
        keyword = keyword.strip()
        if keyword == "最近":
            result = await self.chaoxing_service.get_recent_deadlines()
            yield event.plain_result(format_homework_list(result, "最近截止作业"))
            return
        result = await self.chaoxing_service.get_pending_homework(course_name=keyword)
        yield event.plain_result(format_homework_list(result, "未完成作业"))

    @filter.command("学习通状态")
    async def homework_status(self, event: AstrMessageEvent):
        """查询学习通功能状态。"""
        access_error = self._general_access_error(event)
        if access_error:
            yield event.plain_result(access_error)
            return
        status = await self.chaoxing_service.get_status()
        lines = [
            "学习通状态",
            f"启用状态：{'已启用' if status['enabled'] else '未启用'}",
            f"provider：{status['provider']}",
            f"最近更新：{status['last_update_time'] or '无'}",
        ]
        yield event.plain_result("\n".join(lines))

    @filter.command("学习通刷新")
    async def homework_refresh(self, event: AstrMessageEvent):
        """手动刷新学习通作业缓存。"""
        access_error = self._general_access_error(event)
        if access_error:
            yield event.plain_result(access_error)
            return
        result = await self.chaoxing_service.refresh()
        yield event.plain_result(result["message"])

    @llm_tool("get_today_courses")
    async def tool_get_today_courses(self, _context) -> str:
        """查询今天课程。

        Args:
        """
        return json.dumps(self.course_service.get_today_courses(), ensure_ascii=False)

    @llm_tool("get_tomorrow_courses")
    async def tool_get_tomorrow_courses(self, _context) -> str:
        """查询明天课程。

        Args:
        """
        return json.dumps(self.course_service.get_tomorrow_courses(), ensure_ascii=False)

    @llm_tool("get_next_course")
    async def tool_get_next_course(self, _context) -> str:
        """查询下一节课。

        Args:
        """
        return json.dumps(self.course_service.get_next_course(), ensure_ascii=False)

    @llm_tool("get_current_course")
    async def tool_get_current_course(self, _context) -> str:
        """查询当前课程。

        Args:
        """
        return json.dumps(self.course_service.get_current_course(), ensure_ascii=False)

    @llm_tool("get_course_weeks")
    async def tool_get_course_weeks(self, _context, course_name: str) -> str:
        """查询某门课程上课周次。

        Args:
            course_name(string): 课程名称或关键词
        """
        return json.dumps(self.course_service.get_course_weeks(course_name), ensure_ascii=False)

    @llm_tool("get_empty_classrooms_now")
    async def tool_get_empty_classrooms_now(self, _context, building: str = "") -> str:
        """查询当前可用空教室。

        Args:
            building(string): 可选，教学楼名称
        """
        return json.dumps(await self.classroom_service.get_empty_classrooms_now(building), ensure_ascii=False)

    @llm_tool("get_empty_classrooms_by_time")
    async def tool_get_empty_classrooms_by_time(self, _context, building: str = "", start_time: str = "", end_time: str = "") -> str:
        """查询指定时间段空教室。

        Args:
            building(string): 可选，教学楼名称
            start_time(string): 开始时间，格式 HH:MM 或 YYYY-MM-DD HH:MM
            end_time(string): 结束时间，格式 HH:MM 或 YYYY-MM-DD HH:MM
        """
        if start_time and len(start_time) == 5:
            start_time = combine_date_time(today_local(), start_time).strftime("%Y-%m-%d %H:%M")
        if end_time and len(end_time) == 5:
            end_time = combine_date_time(today_local(), end_time).strftime("%Y-%m-%d %H:%M")
        return json.dumps(
            await self.classroom_service.get_empty_classrooms_by_time(building, start_time, end_time),
            ensure_ascii=False,
        )

    @llm_tool("recommend_study_room")
    async def tool_recommend_study_room(
        self,
        _context,
        start_time: str = "",
        end_time: str = "",
        current_location: str = "",
        next_course_location: str = "",
    ) -> str:
        """推荐自习教室。

        Args:
            start_time(string): 可选，开始时间
            end_time(string): 可选，结束时间
            current_location(string): 可选，当前位置
            next_course_location(string): 可选，下一节课地点
        """
        _ = current_location
        if start_time and len(start_time) == 5:
            start_time = combine_date_time(today_local(), start_time).strftime("%Y-%m-%d %H:%M")
        if end_time and len(end_time) == 5:
            end_time = combine_date_time(today_local(), end_time).strftime("%Y-%m-%d %H:%M")
        result = (
            await self.classroom_service.get_empty_classrooms_by_time(start_time=start_time, end_time=end_time)
            if start_time and end_time
            else await self.classroom_service.get_empty_classrooms_now()
        )
        rows = result["data"] if result["success"] else []
        if not next_course_location:
            next_course = self.course_service.get_next_course()
            next_course_location = next_course["data"]["location"] if next_course["success"] else ""
        data = self.distance_service.recommend(rows, next_course_location=next_course_location)
        return json.dumps({"success": True, "data": data, "message": "已返回推荐结果。"}, ensure_ascii=False)

    @llm_tool("get_pending_homework")
    async def tool_get_pending_homework(self, _context, course_name: str = "") -> str:
        """查询未完成作业。

        Args:
            course_name(string): 可选，课程名
        """
        return json.dumps(await self.chaoxing_service.get_pending_homework(course_name), ensure_ascii=False)

    @llm_tool("get_recent_deadlines")
    async def tool_get_recent_deadlines(self, _context, within_days: int = 7) -> str:
        """查询最近截止作业。

        Args:
            within_days(number): 可选，默认 7 天
        """
        return json.dumps(await self.chaoxing_service.get_recent_deadlines(within_days), ensure_ascii=False)

    @llm_tool("get_plugin_status")
    async def tool_get_plugin_status(self, _context) -> str:
        """查询插件状态。

        Args:
        """
        return json.dumps(await self._build_status(), ensure_ascii=False)
