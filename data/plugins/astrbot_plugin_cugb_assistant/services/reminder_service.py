from __future__ import annotations


class ReminderService:
    def __init__(self, config: dict) -> None:
        self.config = config

    def get_status(self) -> dict:
        reminder_cfg = self.config["reminder"]
        return {
            "enabled": True,
            "class_reminder_enabled": reminder_cfg.get("enable_class_reminder", True),
            "homework_reminder_enabled": reminder_cfg.get("enable_homework_reminder", True),
            "before_class_minutes": reminder_cfg.get("before_class_minutes", 20),
            "job_loaded": False,
            "message": "课前提醒模块已加载，但提醒任务尚未启用。",
        }
