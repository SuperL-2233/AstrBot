from __future__ import annotations

from collections.abc import Awaitable, Callable

from ..repositories.task_repository import TaskRepository


class ReminderService:
    def __init__(self, config: dict, repository: TaskRepository) -> None:
        self.config = config
        self.repository = repository

    def bind(self, target_umo: str) -> None:
        self.repository.set_target_umo(target_umo.strip())

    def unbind(self) -> None:
        self.repository.clear_target_umo()

    def status(self) -> dict:
        return {
            "enabled": bool(self.config.get("reminder_enabled", True)),
            "target_bound": bool(self.repository.get_target_umo()),
            "before_hours": max(1, int(self.config.get("remind_before_hours", 24))),
        }

    async def deliver_due(
        self, send_message: Callable[[str, str], Awaitable[bool]]
    ) -> dict:
        if not self.config.get("reminder_enabled", True):
            return {"success": False, "message": "超星截止提醒未启用。", "sent": 0}
        target = self.repository.get_target_umo()
        if not target:
            return {"success": False, "message": "尚未绑定提醒私聊。", "sent": 0}

        before_hours = max(1, int(self.config.get("remind_before_hours", 24)))
        claimed: list[tuple[str, dict]] = []
        for task in self.repository.due_reminders(before_hours):
            reminder_key = f"{task['task_key']}|{task['deadline']}|{before_hours}"
            if self.repository.claim_reminder(reminder_key):
                claimed.append((reminder_key, task))
        if not claimed:
            return {"success": True, "message": "没有新的到期提醒。", "sent": 0}

        lines = [f"超星学习通截止提醒（未来 {before_hours} 小时）"]
        for _, task in claimed:
            lines.append(
                f"[{task['task_type']}] {task['course_name']} - {task['title']}\n截止：{task['deadline']}"
            )
        try:
            sent = await send_message(target, "\n\n".join(lines))
            if not sent:
                raise RuntimeError("AstrBot 未找到目标会话对应的平台适配器")
        except Exception:
            for reminder_key, _ in claimed:
                self.repository.release_reminder(reminder_key)
            raise
        for reminder_key, _ in claimed:
            self.repository.mark_delivered(reminder_key)
        return {
            "success": True,
            "message": "超星截止提醒已推送。",
            "sent": len(claimed),
        }
