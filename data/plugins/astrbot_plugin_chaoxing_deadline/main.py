from __future__ import annotations

import asyncio
import json
from contextlib import suppress

from astrbot.api import llm_tool, logger, star
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.core.star.filter.command import GreedyStr

from .formatters.task_formatter import (
    format_status,
    format_task_tool_result,
    format_tasks,
)
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
    "0.4.3",
)
class ChaoxingDeadlinePlugin(star.Star):
    def __init__(self, context: star.Context, config: dict | None = None) -> None:
        super().__init__(context, config)
        self.config = load_plugin_config(config)
        repository = TaskRepository(resolve_data_path(self.config["database"]))
        self.provider = ChaoxingProvider(self.config, repository)
        self.reminder_service = ReminderService(self.config, repository)
        self.task_service = TaskService(
            self.config,
            self.provider,
            repository,
            on_refreshed=self.reminder_service.sync_schedule,
        )
        self._refresh_task: asyncio.Task | None = None
        self._reminder_task: asyncio.Task | None = None

    async def initialize(self) -> None:
        scheduled = self.reminder_service.sync_schedule()
        logger.info(f"Chaoxing reminder schedule synchronized: {scheduled} new stages")
        if self.config.get("enabled", False):
            self._refresh_task = asyncio.create_task(self._run_refresh_loop())
            logger.info("Chaoxing synchronization loop started")
        else:
            logger.info("Chaoxing online synchronization is disabled")
        if self.config.get("reminder_enabled", True):
            self._reminder_task = asyncio.create_task(self._run_reminder_loop())
            logger.info("Chaoxing reminder scan loop started")
            if not self.reminder_service.status()["target_bound"]:
                logger.warning(
                    "Chaoxing reminders have no target; bind the intended QQ private "
                    "chat with /超星提醒绑定 (weather binding is independent)"
                )

    async def terminate(self) -> None:
        if self._refresh_task:
            self._refresh_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._refresh_task
        # A running sync is read-only; never stall a WebUI disable/reload on it.
        await self.provider.wait_for_worker(timeout=5)
        if self._reminder_task:
            self._reminder_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._reminder_task

    async def _run_refresh_loop(self) -> None:
        interval = max(5, int(self.config.get("refresh_interval_minutes", 120))) * 60
        while True:
            try:
                freshness = await self.task_service.ensure_fresh()
                result = freshness.get("refresh_result")
                if result and result["success"]:
                    logger.info(result["message"])
                elif result:
                    logger.warning(result["message"])
            except Exception as exc:
                logger.warning(f"Chaoxing refresh loop failed: {exc}")
            await asyncio.sleep(interval)

    async def _run_reminder_loop(self) -> None:
        interval = (
            max(1, int(self.config.get("reminder_scan_interval_minutes", 5))) * 60
        )
        while True:
            try:
                result = await self.reminder_service.deliver_due(self._send_message)
                if result.get("sent"):
                    logger.info(result["message"])
            except Exception as exc:
                logger.warning(f"Chaoxing reminder scan failed: {exc}")
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
            "reminder_loop_running": bool(
                self._reminder_task and not self._reminder_task.done()
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
                "查询前 24 小时缓存新鲜度检查与自动同步",
                "截止前 3 天、2 天、1 天、12 小时、1 小时的 QQ 私聊提醒",
                "新发现任务和重启后补发当前阶段，失败可重试",
                "按任务取消或恢复提醒跟踪",
            ],
            "commands": [
                "/超星任务",
                "/超星已完成",
                "/超星全部",
                "/超星截止",
                "/超星同步",
                "/超星忽略",
                "/超星恢复",
                "/超星状态",
                "/超星提醒绑定",
                "/超星提醒解绑",
            ],
            "llm_tools": [
                "get_chaoxing_tasks",
                "get_chaoxing_deadlines",
                "sync_chaoxing_tasks",
                "set_chaoxing_task_tracking",
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
            "/超星忽略 [课程或任务关键词]\n"
            "/超星恢复 [课程或任务关键词]\n"
            "/超星提醒绑定\n"
            "/超星提醒解绑\n"
            "默认在截止前 3 天、2 天、1 天、12 小时、1 小时提醒；"
            "天气推送和超星提醒需要分别绑定。"
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
                status["reminder_loop_running"],
            )
        )

    @filter.command("超星同步")
    async def refresh_command(self, event: AstrMessageEvent):
        """Reply after the login phase and push the completion to this chat."""
        umo = event.unified_msg_origin

        async def push_result(result: dict) -> None:
            try:
                await self._send_message(umo, result["message"])
            except Exception as exc:
                logger.warning(f"超星后台同步结果推送失败: {exc}")

        result = await self.task_service.refresh_detached(on_finished=push_result)
        yield event.plain_result(result["message"])

    @filter.command("超星任务")
    async def tasks_command(
        self, event: AstrMessageEvent, task_type: str = "全部", keyword: GreedyStr = ""
    ):
        """Query pending homework, video chapters, or discussions."""
        normalized_type = "" if task_type.strip() == "全部" else task_type.strip()
        result = await self.task_service.query_tasks(
            normalized_type, keyword.strip(), task_status="待办"
        )
        yield event.plain_result(format_tasks(result, "超星待办任务"))

    @filter.command("超星已完成")
    async def completed_tasks_command(
        self, event: AstrMessageEvent, task_type: str = "全部", keyword: GreedyStr = ""
    ):
        """Query completed homework, video chapters, or discussions."""
        normalized_type = "" if task_type.strip() == "全部" else task_type.strip()
        result = await self.task_service.query_tasks(
            normalized_type, keyword.strip(), task_status="已完成"
        )
        yield event.plain_result(format_tasks(result, "超星已完成任务"))

    @filter.command("超星全部")
    async def all_tasks_command(
        self, event: AstrMessageEvent, task_type: str = "全部", keyword: GreedyStr = ""
    ):
        """Query all cached homework, video chapters, or discussions."""
        normalized_type = "" if task_type.strip() == "全部" else task_type.strip()
        result = await self.task_service.query_tasks(
            normalized_type, keyword.strip(), task_status="全部"
        )
        yield event.plain_result(format_tasks(result, "超星全部任务"))

    @filter.command("超星截止")
    async def deadlines_command(self, event: AstrMessageEvent, within_days: int = 7):
        """Query tasks with explicit deadlines in a bounded future window."""
        result = await self.task_service.query_deadlines(within_days)
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

    @filter.command("超星忽略")
    async def ignore_task(self, event: AstrMessageEvent, keyword: GreedyStr = ""):
        """Stop reminder tracking for one unambiguous dated task."""
        result = self.reminder_service.set_tracking(
            tracked=False, keyword=keyword.strip(), reason="用户通过命令取消跟踪"
        )
        yield event.plain_result(result["message"])

    @filter.command("超星恢复")
    async def restore_task(self, event: AstrMessageEvent, keyword: GreedyStr = ""):
        """Restore reminder tracking for one unambiguous dated task."""
        result = self.reminder_service.set_tracking(
            tracked=True, keyword=keyword.strip()
        )
        yield event.plain_result(result["message"])

    @llm_tool("get_chaoxing_tasks")
    async def tool_get_chaoxing_tasks(
        self,
        _context,
        task_type: str = "",
        task_status: str = "待办",
        keyword: str = "",
    ) -> str:
        """Query 学习通 tasks by type, completion state and keyword.

        Args:
            task_type(string): 作业/视频/限时章节/讨论; blank queries all.
            task_status(string): 待办 (default), 已完成 or 全部.
            keyword(string): Optional course/title keyword.

        Returns:
            JSON with the query or operation result.
        """
        return format_task_tool_result(
            await self.task_service.query_tasks(
                task_type.strip(), keyword.strip(), task_status.strip()
            )
        )

    @llm_tool("get_chaoxing_deadlines")
    async def tool_get_chaoxing_deadlines(self, _context, within_days: int = 7) -> str:
        """Get pending 学习通 tasks with explicit deadlines in the next specified days.

        Args:
            within_days(number): Days ahead, 0-365; default 7.

        Returns:
            JSON with the query or operation result.
        """
        return format_task_tool_result(
            await self.task_service.query_deadlines(within_days)
        )

    @llm_tool("sync_chaoxing_tasks")
    async def tool_sync_chaoxing_tasks(self, _context) -> str:
        """Log in to 学习通; sync tasks/reminders in background. Return after login and
        push completion here. Do not repeat before completion.

        Returns:
            JSON with the query or operation result.
        """
        umo = str(getattr(_context, "unified_msg_origin", "") or "")

        async def push_result(result: dict) -> None:
            target = umo or self.reminder_service.repository.get_target_umo()
            if not target:
                logger.warning(f"超星后台同步结束但无推送目标：{result['message']}")
                return
            try:
                await self._send_message(target, result["message"])
            except Exception as exc:
                logger.warning(f"超星后台同步结果推送失败: {exc}")

        result = await self.task_service.refresh_detached(on_finished=push_result)
        return json.dumps(result, ensure_ascii=False, separators=(",", ":"))

    @llm_tool("set_chaoxing_task_tracking")
    async def tool_set_chaoxing_task_tracking(
        self,
        _context,
        tracked: bool,
        task_key: str = "",
        keyword: str = "",
        reason: str = "",
    ) -> str:
        """Cancel or restore 学习通 deadline reminders. Prefer task_key; ambiguous
        keywords return candidates without changes.

        Args:
            tracked(boolean): false cancels reminders; true restores them.
            task_key(string): Optional exact key from task queries.
            keyword(string): Optional course/title keyword.
            reason(string): Optional cancellation reason.

        Returns:
            JSON with the query or operation result.
        """
        return format_task_tool_result(
            self.reminder_service.set_tracking(
                tracked=tracked,
                task_key=task_key,
                keyword=keyword,
                reason=reason,
            )
        )
