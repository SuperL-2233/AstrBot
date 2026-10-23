from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import datetime
from zoneinfo import ZoneInfo

from ..repositories.task_repository import TaskRepository
from ..utils.config_loader import DEFAULT_CONFIG


class ReminderService:
    DEFAULT_OFFSETS_HOURS = DEFAULT_CONFIG["reminder_offsets_hours"]

    def __init__(self, config: dict, repository: TaskRepository) -> None:
        self.config = config
        self.repository = repository
        self.timezone = ZoneInfo(config.get("timezone", "Asia/Shanghai"))

    def bind(self, target_umo: str) -> None:
        self.repository.set_target_umo(target_umo.strip())

    def unbind(self) -> None:
        self.repository.clear_target_umo()

    def status(self) -> dict:
        result = {
            "enabled": bool(self.config.get("reminder_enabled", True)),
            "target_bound": bool(self.repository.get_target_umo()),
            "offsets_hours": self.offsets_hours(),
            "timezone": str(self.timezone),
            "scan_interval_minutes": max(
                1, int(self.config.get("reminder_scan_interval_minutes", 5))
            ),
        }
        result.update(self.repository.reminder_schedule_status())
        return result

    def offsets_hours(self) -> list[int]:
        values = self.config.get("reminder_offsets_hours", self.DEFAULT_OFFSETS_HOURS)
        if not isinstance(values, list):
            values = self.DEFAULT_OFFSETS_HOURS
        normalized = {
            int(value)
            for value in values
            if str(value).strip().isdigit() and int(value) > 0
        }
        return sorted(normalized or self.DEFAULT_OFFSETS_HOURS, reverse=True)

    def sync_schedule(self, now: datetime | None = None) -> int:
        """Reconcile future stages and the latest elapsed stage in local time.

        Args:
            now: Optional current time; naive values use the configured timezone.

        Returns:
            Number of newly scheduled stages.
        """
        current = now or datetime.now(self.timezone)
        if current.tzinfo is not None:
            current = current.astimezone(self.timezone).replace(tzinfo=None)
        return self.repository.sync_reminder_schedule(self.offsets_hours(), current)

    def set_tracking(
        self,
        *,
        tracked: bool,
        task_key: str = "",
        keyword: str = "",
        reason: str = "",
    ) -> dict:
        if not task_key.strip() and not keyword.strip():
            return {
                "success": False,
                "data": [],
                "message": "请提供任务 task_key 或课程/任务关键词。",
                "error_code": "TASK_SELECTOR_REQUIRED",
            }
        matches = self.repository.find_trackable_tasks(
            keyword.strip(), task_key.strip()
        )
        if not matches:
            return {
                "success": False,
                "data": [],
                "message": "没有找到带明确截止时间的匹配任务。",
                "error_code": "TRACKABLE_TASK_NOT_FOUND",
            }
        if len(matches) > 1:
            candidates = [
                {
                    key: row[key]
                    for key in (
                        "task_key",
                        "task_type",
                        "course_name",
                        "title",
                        "deadline",
                        "tracked",
                    )
                }
                for row in matches
            ]
            return {
                "success": False,
                "data": candidates,
                "message": "匹配到多个任务，请使用返回的 task_key 精确选择。",
                "error_code": "AMBIGUOUS_TASK_SELECTOR",
            }
        task = matches[0]
        self.repository.set_task_ignored(task["task_key"], not tracked, reason.strip())
        if tracked:
            self.sync_schedule()
        action = "恢复跟踪" if tracked else "取消跟踪"
        return {
            "success": True,
            "data": {
                "task_key": task["task_key"],
                "task_type": task["task_type"],
                "course_name": task["course_name"],
                "title": task["title"],
                "deadline": task["deadline"],
                "tracked": tracked,
            },
            "message": f"已{action}：{task['course_name']} - {task['title']}。",
            "error_code": None,
        }

    async def deliver_due(
        self,
        send_message: Callable[[str, str], Awaitable[bool]],
        now: datetime | None = None,
    ) -> dict:
        """Send due stages once, retaining failed or cancelled sends for retry.

        Args:
            send_message: AstrBot delivery callback returning adapter success.
            now: Optional current time; naive values use the configured timezone.

        Returns:
            Delivery status and the number of stages sent.

        Raises:
            Exception: If the delivery callback fails or returns false.
            asyncio.CancelledError: If delivery is cancelled during shutdown.
        """
        if not self.config.get("reminder_enabled", True):
            return {"success": False, "message": "超星截止提醒未启用。", "sent": 0}
        current = now or datetime.now(self.timezone)
        if current.tzinfo is not None:
            current = current.astimezone(self.timezone).replace(tzinfo=None)
        self.sync_schedule(current)
        target = self.repository.get_target_umo()
        if not target:
            return {"success": False, "message": "尚未绑定提醒私聊。", "sent": 0}

        claimed: list[tuple[str, dict]] = []
        for task in self.repository.due_scheduled_reminders(current):
            reminder_key = task["schedule_key"]
            if self.repository.claim_reminder(reminder_key):
                claimed.append((reminder_key, task))
        if not claimed:
            return {"success": True, "message": "没有新的到期提醒。", "sent": 0}

        lines = ["超星学习通截止提醒"]
        for _, task in claimed:
            offset = int(task["offset_hours"])
            stage = (
                f"{offset // 24} 天"
                if offset >= 24 and offset % 24 == 0
                else f"{offset} 小时"
            )
            remaining = (
                datetime.strptime(task["deadline"], "%Y-%m-%d %H:%M:%S") - current
            )
            remaining_minutes = max(1, int(remaining.total_seconds() / 60))
            lines.append(
                f"[{task['task_type']}][截止前 {stage}] "
                f"{task['course_name']} - {task['title']}\n"
                f"截止：{task['deadline']}（{self.timezone}）\n"
                f"当前剩余：{remaining_minutes // 60} 小时 {remaining_minutes % 60} 分钟"
            )
        try:
            sent = await send_message(target, "\n\n".join(lines))
            if not sent:
                raise RuntimeError("AstrBot could not find the target platform adapter")
        except (Exception, asyncio.CancelledError):
            for reminder_key, _ in claimed:
                self.repository.release_reminder(reminder_key)
            raise
        for reminder_key, _ in claimed:
            self.repository.mark_delivered(reminder_key)
            self.repository.mark_schedule_delivered(reminder_key)
        return {
            "success": True,
            "message": "超星截止提醒已推送。",
            "sent": len(claimed),
        }
