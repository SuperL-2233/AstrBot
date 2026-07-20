from __future__ import annotations

from ..repositories.homework_repo import HomeworkRepository
from ..utils.result import make_result


class ChaoxingCacheProvider:
    """Read homework previously stored in the local SQLite cache."""

    def __init__(self, repo: HomeworkRepository) -> None:
        self.repo = repo

    async def get_pending_homework(self, course_name: str = "") -> dict:
        rows = self.repo.query_pending(course_name=course_name)
        return make_result(
            True,
            data=rows,
            message="已返回本地缓存中的作业数据。",
            source_status=await self.get_status(),
        )

    async def get_recent_deadlines(self, within_days: int = 7) -> dict:
        _ = within_days
        rows = self.repo.query_recent()
        return make_result(
            True,
            data=rows,
            message="已返回本地缓存中的作业数据。",
            source_status=await self.get_status(),
        )

    async def refresh(self) -> dict:
        return make_result(
            False,
            data=None,
            message="当前只支持读取本地作业缓存，尚未实现学习通在线刷新。",
            error_code="CHAOXING_ONLINE_REFRESH_NOT_IMPLEMENTED",
            source_status=await self.get_status(),
        )

    async def get_status(self) -> dict:
        return {
            "enabled": True,
            "provider": "cache",
            "last_update_time": self.repo.last_update_time(),
            "count": self.repo.count(),
            "online_refresh_implemented": False,
        }
