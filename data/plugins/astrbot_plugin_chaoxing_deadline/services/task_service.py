from __future__ import annotations

from ..providers.chaoxing_provider import ChaoxingProvider
from ..repositories.task_repository import TaskRepository


class TaskService:
    TASK_TYPES = {"作业", "视频", "讨论"}
    TASK_STATUSES = {"待办": "pending", "已完成": "completed", "全部": ""}

    def __init__(
        self, config: dict, provider: ChaoxingProvider, repository: TaskRepository
    ) -> None:
        self.config = config
        self.provider = provider
        self.repository = repository

    async def refresh(self) -> dict:
        if not self.config.get("enabled", False):
            return {
                "success": False,
                "data": None,
                "message": "超星在线同步未启用。",
                "error_code": "CHAOXING_DISABLED",
            }
        return await self.provider.refresh()

    def get_tasks(
        self, task_type: str = "", keyword: str = "", task_status: str = "待办"
    ) -> dict:
        if task_type and task_type not in self.TASK_TYPES:
            return {
                "success": False,
                "data": [],
                "message": "任务类型仅支持：作业、视频、讨论。",
                "error_code": "INVALID_TASK_TYPE",
            }
        if task_status not in self.TASK_STATUSES:
            return {
                "success": False,
                "data": [],
                "message": "任务状态仅支持：待办、已完成、全部。",
                "error_code": "INVALID_TASK_STATUS",
            }
        return {
            "success": True,
            "data": self.repository.query_tasks(
                task_type, keyword, self.TASK_STATUSES[task_status]
            ),
            "message": f"已返回本地同步缓存中的{task_status}任务。",
            "error_code": None,
        }

    def get_deadlines(self, within_days: int = 7) -> dict:
        if within_days < 0 or within_days > 365:
            return {
                "success": False,
                "data": [],
                "message": "查询天数必须在 0 到 365 之间。",
                "error_code": "INVALID_WITHIN_DAYS",
            }
        return {
            "success": True,
            "data": self.repository.query_deadlines(within_days),
            "message": f"已返回未来 {within_days} 天内有明确截止时间的任务。",
            "error_code": None,
        }

    def status(self) -> dict:
        return self.provider.status()
