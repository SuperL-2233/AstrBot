from __future__ import annotations

import asyncio
import json
from contextlib import suppress

from astrbot.api import llm_tool, logger, star
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.core.star.filter.command import GreedyStr

from .formatters.task_formatter import format_status, format_tasks
from .providers.chaoxing_provider import ChaoxingProvider
from .repositories.task_repository import TaskRepository
from .services.reminder_service import ReminderService
from .services.task_service import TaskService
from .utils.config_loader import load_plugin_config
from .utils.paths import resolve_data_path
from .utils.permission import ensure_private_chat


@star.register(
    "astrbot_plugin_chaoxing_deadline",
    "macroladlezilla",
    "超星学习通助手",
    "0.3.0",
)
class ChaoxingDeadlinePlugin(star.Star):
    def __init__(self, context: star.Context, config: dict | None = None) -> None:
        super().__init__(context, config)
        self.config = load_plugin_config(config)
        repository = TaskRepository(resolve_data_path(self.config["database"]))
        self.provider = ChaoxingProvider(self.config, repository)
        self.task_service = TaskService(self.config, self.provider, repository)
        self.reminder_service = ReminderService(self.config, repository)
        self._refresh_task: asyncio.Task | None = None

    async def initialize(self) -> None:
        if self.config.get("enabled", False):
            self._refresh_task = asyncio.create_task(self._run_refresh_loop())
            logger.info("Chaoxing deadline synchronization loop started")
        else:
            logger.info("Chaoxing deadline synchronization is disabled")

    async def terminate(self) -> None:
        if self._refresh_task:
            self._refresh_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._refresh_task

    async def _run_refresh_loop(self) -> None:
        interval = max(5, int(self.config.get("refresh_interval_minutes", 120))) * 60
        while True:
            try:
                result = await self.task_service.refresh()
                if result["success"]:
                    logger.info(result["message"])
                else:
                    logger.warning(result["message"])
                reminder_result = await self.reminder_service.deliver_due(
                    self._send_message
                )
                if reminder_result.get("sent"):
                    logger.info(reminder_result["message"])
            except Exception as exc:
                logger.warning(f"Chaoxing refresh or reminder loop failed: {exc}")
            await asyncio.sleep(interval)

    async def _send_message(self, target_umo: str, text: str) -> bool:
        return await self.context.send_message(target_umo, MessageChain().message(text))

    def get_public_status(self) -> dict:
        """Return Chaoxing state for the capability registry."""
        return {
            "task": self.task_service.status(),
            "reminder": self.reminder_service.status(),
            "refresh_loop_running": bool(
                self._refresh_task and not self._refresh_task.done()
            ),
        }

    def get_public_capabilities(self) -> dict:
        """Describe public Chaoxing features using the project plugin contract."""
        return {
            "summary": "查询学习通任务记录、明确截止时间并推送截止提醒。",
            "features": [
                "待办、已完成和全部任务记录查询",
                "作业、视频章节和讨论分类查询",
                "明确截止时间查询",
                "QQ 私聊截止提醒",
            ],
            "commands": [
                "/超星任务",
                "/超星已完成",
                "/超星全部",
                "/超星截止",
                "/超星同步",
                "/超星状态",
            ],
            "llm_tools": [
                "get_chaoxing_tasks",
                "get_chaoxing_homeworks",
                "get_chaoxing_video_chapters",
                "get_chaoxing_discussions",
                "get_chaoxing_deadlines",
            ],
        }

    @filter.command("超星帮助")
    async def help_command(self, event: AstrMessageEvent):
        """Show commands for the independent Chaoxing plugin."""
        yield event.plain_result(
            "超星学习通截止提醒\n"
            "/超星状态\n"
            "/超星同步\n"
            "/超星任务 [全部|作业|视频|讨论] [关键词]（待办）\n"
            "/超星已完成 [全部|作业|视频|讨论] [关键词]\n"
            "/超星全部 [全部|作业|视频|讨论] [关键词]\n"
            "/超星截止 [未来天数]\n"
            "/超星提醒绑定\n"
            "/超星提醒解绑"
        )

    @filter.command("超星状态")
    async def status_command(self, event: AstrMessageEvent):
        """Show synchronization, cache, and reminder readiness."""
        status = self.get_public_status()
        yield event.plain_result(
            format_status(
                status["task"],
                status["reminder"],
                status["refresh_loop_running"],
            )
        )

    @filter.command("超星同步")
    async def refresh_command(self, event: AstrMessageEvent):
        """Manually synchronize all supported read-only task types."""
        result = await self.task_service.refresh()
        yield event.plain_result(result["message"])

    @filter.command("超星任务")
    async def tasks_command(
        self, event: AstrMessageEvent, task_type: str = "全部", keyword: GreedyStr = ""
    ):
        """Query pending homework, video chapters, or discussions."""
        normalized_type = "" if task_type.strip() == "全部" else task_type.strip()
        result = self.task_service.get_tasks(
            normalized_type, keyword.strip(), task_status="待办"
        )
        yield event.plain_result(format_tasks(result, "超星待办任务"))

    @filter.command("超星已完成")
    async def completed_tasks_command(
        self, event: AstrMessageEvent, task_type: str = "全部", keyword: GreedyStr = ""
    ):
        """Query completed homework, video chapters, or discussions."""
        normalized_type = "" if task_type.strip() == "全部" else task_type.strip()
        result = self.task_service.get_tasks(
            normalized_type, keyword.strip(), task_status="已完成"
        )
        yield event.plain_result(format_tasks(result, "超星已完成任务"))

    @filter.command("超星全部")
    async def all_tasks_command(
        self, event: AstrMessageEvent, task_type: str = "全部", keyword: GreedyStr = ""
    ):
        """Query all cached homework, video chapters, or discussions."""
        normalized_type = "" if task_type.strip() == "全部" else task_type.strip()
        result = self.task_service.get_tasks(
            normalized_type, keyword.strip(), task_status="全部"
        )
        yield event.plain_result(format_tasks(result, "超星全部任务"))

    @filter.command("超星截止")
    async def deadlines_command(self, event: AstrMessageEvent, within_days: int = 7):
        """Query tasks with explicit deadlines in a bounded future window."""
        result = self.task_service.get_deadlines(within_days)
        yield event.plain_result(
            format_tasks(result, f"未来 {within_days} 天超星截止任务")
        )

    @filter.command("超星提醒绑定")
    async def bind_reminder(self, event: AstrMessageEvent):
        """Bind proactive reminders to the owner's current private chat."""
        access_error = ensure_private_chat(event)
        if access_error:
            yield event.plain_result(access_error)
            return
        self.reminder_service.bind(event.unified_msg_origin)
        yield event.plain_result("已将超星截止提醒绑定到当前 QQ 私聊。")

    @filter.command("超星提醒解绑")
    async def unbind_reminder(self, event: AstrMessageEvent):
        """Remove the persisted proactive reminder destination."""
        access_error = ensure_private_chat(event)
        if access_error:
            yield event.plain_result(access_error)
            return
        self.reminder_service.unbind()
        yield event.plain_result("已解除超星截止提醒绑定。")

    @llm_tool("get_chaoxing_tasks")
    async def tool_get_chaoxing_tasks(
        self,
        _context,
        task_type: str = "",
        task_status: str = "待办",
        keyword: str = "",
    ) -> str:
        """查询学习通作业、视频章节和讨论，可包含已完成记录。

        Args:
            task_type(string): 可选，作业、视频或讨论；留空查询全部
            task_status(string): 待办、已完成或全部，默认待办
            keyword(string): 可选，课程名或任务名关键词
        """
        return json.dumps(
            self.task_service.get_tasks(
                task_type.strip(), keyword.strip(), task_status.strip()
            ),
            ensure_ascii=False,
        )

    def _category_tasks(self, task_type: str, task_status: str, keyword: str) -> str:
        return json.dumps(
            self.task_service.get_tasks(
                task_type, keyword.strip(), task_status.strip()
            ),
            ensure_ascii=False,
        )

    @llm_tool("get_chaoxing_homeworks")
    async def tool_get_chaoxing_homeworks(
        self, _context, task_status: str = "待办", keyword: str = ""
    ) -> str:
        """查询学习通作业，包括待办、已完成或全部记录。

        Args:
            task_status(string): 待办、已完成或全部，默认待办
            keyword(string): 可选，课程名或作业名关键词
        """
        return self._category_tasks("作业", task_status, keyword)

    @llm_tool("get_chaoxing_video_chapters")
    async def tool_get_chaoxing_video_chapters(
        self, _context, task_status: str = "待办", keyword: str = ""
    ) -> str:
        """查询学习通视频章节，包括待办、已完成或全部记录。

        Args:
            task_status(string): 待办、已完成或全部，默认待办
            keyword(string): 可选，课程名或章节名关键词
        """
        return self._category_tasks("视频", task_status, keyword)

    @llm_tool("get_chaoxing_discussions")
    async def tool_get_chaoxing_discussions(
        self, _context, task_status: str = "待办", keyword: str = ""
    ) -> str:
        """查询学习通讨论，包括待办、已完成或全部记录。

        Args:
            task_status(string): 待办、已完成或全部，默认待办
            keyword(string): 可选，课程名或讨论标题关键词
        """
        return self._category_tasks("讨论", task_status, keyword)

    @llm_tool("get_chaoxing_deadlines")
    async def tool_get_chaoxing_deadlines(self, _context, within_days: int = 7) -> str:
        """查询未来一段时间内有明确截止时间的学习通任务。

        Args:
            within_days(number): 查询未来天数，范围 0 到 365
        """
        return json.dumps(
            self.task_service.get_deadlines(within_days), ensure_ascii=False
        )
