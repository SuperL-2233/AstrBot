from __future__ import annotations

import asyncio
from contextlib import suppress

from astrbot.api import logger, star
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.core.star.filter.command import GreedyStr

from .formatters.task_formatter import format_status, format_tasks
from .providers.chaoxing_provider import ChaoxingProvider
from .repositories.task_repository import TaskRepository
from .services.reminder_service import ReminderService
from .services.task_service import TaskService
from .utils.config_loader import load_plugin_config
from .utils.paths import resolve_data_path
from .utils.permission import ensure_owner


@star.register(
    "astrbot_plugin_chaoxing_deadline",
    "macroladlezilla",
    "超星学习通截止提醒",
    "0.1.0",
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

    @filter.command("超星帮助")
    async def help_command(self, event: AstrMessageEvent):
        """Show commands for the independent Chaoxing plugin."""
        access_error = ensure_owner(event, self.config)
        if access_error:
            yield event.plain_result(access_error)
            return
        yield event.plain_result(
            "超星学习通截止提醒\n"
            "/超星状态\n"
            "/超星同步\n"
            "/超星任务 [全部|作业|视频|讨论] [课程或任务关键词]\n"
            "/超星截止 [未来天数]\n"
            "/超星提醒绑定\n"
            "/超星提醒解绑"
        )

    @filter.command("超星状态")
    async def status_command(self, event: AstrMessageEvent):
        """Show synchronization, cache, and reminder readiness."""
        access_error = ensure_owner(event, self.config)
        if access_error:
            yield event.plain_result(access_error)
            return
        yield event.plain_result(
            format_status(
                self.task_service.status(),
                self.reminder_service.status(),
                bool(self._refresh_task and not self._refresh_task.done()),
            )
        )

    @filter.command("超星同步")
    async def refresh_command(self, event: AstrMessageEvent):
        """Manually synchronize all supported read-only task types."""
        access_error = ensure_owner(event, self.config)
        if access_error:
            yield event.plain_result(access_error)
            return
        result = await self.task_service.refresh()
        yield event.plain_result(result["message"])

    @filter.command("超星任务")
    async def tasks_command(
        self, event: AstrMessageEvent, task_type: str = "全部", keyword: GreedyStr = ""
    ):
        """Query pending homework, video chapters, or discussions."""
        access_error = ensure_owner(event, self.config)
        if access_error:
            yield event.plain_result(access_error)
            return
        normalized_type = "" if task_type.strip() == "全部" else task_type.strip()
        result = self.task_service.get_tasks(normalized_type, keyword.strip())
        yield event.plain_result(format_tasks(result, "超星待办任务"))

    @filter.command("超星截止")
    async def deadlines_command(self, event: AstrMessageEvent, within_days: int = 7):
        """Query tasks with explicit deadlines in a bounded future window."""
        access_error = ensure_owner(event, self.config)
        if access_error:
            yield event.plain_result(access_error)
            return
        result = self.task_service.get_deadlines(within_days)
        yield event.plain_result(
            format_tasks(result, f"未来 {within_days} 天超星截止任务")
        )

    @filter.command("超星提醒绑定")
    async def bind_reminder(self, event: AstrMessageEvent):
        """Bind proactive reminders to the owner's current private chat."""
        access_error = ensure_owner(event, self.config, private_only=True)
        if access_error:
            yield event.plain_result(access_error)
            return
        self.reminder_service.bind(event.unified_msg_origin)
        yield event.plain_result("已将超星截止提醒绑定到当前 QQ 私聊。")

    @filter.command("超星提醒解绑")
    async def unbind_reminder(self, event: AstrMessageEvent):
        """Remove the persisted proactive reminder destination."""
        access_error = ensure_owner(event, self.config, private_only=True)
        if access_error:
            yield event.plain_result(access_error)
            return
        self.reminder_service.unbind()
        yield event.plain_result("已解除超星截止提醒绑定。")
