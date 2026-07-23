from __future__ import annotations

import asyncio
import inspect
from collections.abc import Callable
from typing import Any

from ..providers.chaoxing_provider import ChaoxingProvider
from ..repositories.task_repository import TaskRepository


class TaskService:
    TASK_TYPES = {"作业", "视频", "限时章节", "讨论"}
    TASK_STATUSES = {"待办": "pending", "已完成": "completed", "全部": ""}

    def __init__(
        self,
        config: dict,
        provider: ChaoxingProvider,
        repository: TaskRepository,
        on_refreshed: Callable[[], Any] | None = None,
    ) -> None:
        self.config = config
        self.provider = provider
        self.repository = repository
        self.on_refreshed = on_refreshed
        self._refresh_lock = asyncio.Lock()

    async def refresh(self) -> dict:
        if not self.config.get("enabled", False):
            return {
                "success": False,
                "data": None,
                "message": "超星在线同步未启用。",
                "error_code": "CHAOXING_DISABLED",
            }
        async with self._refresh_lock:
            result = await self.provider.refresh()
            if result["success"] and self.on_refreshed:
                callback_result = self.on_refreshed()
                if inspect.isawaitable(callback_result):
                    await callback_result
            return result

    async def ensure_fresh(self) -> dict:
        max_age_hours = max(1, int(self.config.get("cache_max_age_hours", 24)))
        cache = self.repository.cache_state(max_age_hours)
        if not cache["stale"]:
            return {
                "attempted": False,
                "refreshed": False,
                "cache": cache,
                "refresh_result": None,
            }
        async with self._refresh_lock:
            cache = self.repository.cache_state(max_age_hours)
            if not cache["stale"]:
                return {
                    "attempted": False,
                    "refreshed": False,
                    "cache": cache,
                    "refresh_result": None,
                }
            if not self.config.get("enabled", False):
                refresh_result = {
                    "success": False,
                    "data": None,
                    "message": "缓存已超过刷新期限，但超星在线同步未启用。",
                    "error_code": "CHAOXING_DISABLED",
                }
            else:
                refresh_result = await self.provider.refresh()
                if refresh_result["success"] and self.on_refreshed:
                    callback_result = self.on_refreshed()
                    if inspect.isawaitable(callback_result):
                        await callback_result
            return {
                "attempted": True,
                "refreshed": bool(refresh_result["success"]),
                "cache": self.repository.cache_state(max_age_hours),
                "refresh_result": refresh_result,
            }

    @staticmethod
    def _attach_refresh(result: dict, refresh: dict) -> dict:
        result["cache_refresh"] = refresh
        refresh_result = refresh.get("refresh_result")
        if refresh_result and not refresh_result["success"]:
            result["message"] += f" 自动刷新未成功：{refresh_result['message']}"
        elif refresh.get("refreshed"):
            result["message"] += " 查询前已自动刷新云端缓存。"
        return result

    async def query_tasks(
        self, task_type: str = "", keyword: str = "", task_status: str = "待办"
    ) -> dict:
        validation = self.get_tasks(task_type, keyword, task_status)
        if not validation["success"]:
            return validation
        refresh = await self.ensure_fresh()
        return self._attach_refresh(
            self.get_tasks(task_type, keyword, task_status), refresh
        )

    async def query_deadlines(self, within_days: int = 7) -> dict:
        validation = self.get_deadlines(within_days)
        if not validation["success"]:
            return validation
        refresh = await self.ensure_fresh()
        return self._attach_refresh(self.get_deadlines(within_days), refresh)

    def get_tasks(
        self, task_type: str = "", keyword: str = "", task_status: str = "待办"
    ) -> dict:
        if task_type and task_type not in self.TASK_TYPES:
            return {
                "success": False,
                "data": [],
                "message": "任务类型仅支持：作业、视频、限时章节、讨论。",
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
        result = self.provider.status()
        result["cache"] = self.repository.cache_state(
            max(1, int(self.config.get("cache_max_age_hours", 24)))
        )
        return result
